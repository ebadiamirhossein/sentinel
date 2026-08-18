"""Deterministic candlestick rendering (M3).

The same OHLCV must always produce a byte-identical PNG, because a stored signal
has to be reproducible months later. Three things make that true:

* the ``Agg`` backend and matplotlib's bundled DejaVu Sans — no dependency on the
  display stack or on system fonts;
* PNG metadata stripped — matplotlib otherwise stamps ``Software`` and a
  creation date into the header, which alone would break byte-equality;
* **no wall-clock anywhere in the drawing path** — every label, including the
  watermark, is derived from the candle data.

Byte-identity holds for a given matplotlib/mplfinance build, which is why both
are pinned exactly in pyproject.toml.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime
from decimal import Decimal

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from sentinel.charts import theme
from sentinel.charts.models import (
    RENDERER_VERSION,
    ChartImage,
    ChartRenderParams,
    ChartSpec,
    DrawnLevel,
)
from sentinel.core.logging import get_logger
from sentinel.features import indicators
from sentinel.features.models import Level, LevelKind, SymbolFeatures
from sentinel.ingestion.models import DataQuality, MarketSnapshot, OHLCVSeries

log = get_logger(__name__)

#: matplotlib writes `Software` and `Creation Time` into the PNG header by
#: default; both must be suppressed or two identical renders differ in bytes.
_PNG_METADATA: dict[str, str | None] = {"Software": None, "Creation Time": None}


def render(
    series: OHLCVSeries,
    spec: ChartSpec,
    *,
    features: SymbolFeatures | None = None,
    levels: tuple[Level, ...] = (),
    data_quality: DataQuality = DataQuality.OK,
    degraded_fields: tuple[str, ...] = (),
    drop_partial: bool = True,
    now: datetime | None = None,
) -> ChartImage:
    """Render one timeframe. ``now`` is only used to identify a partial candle."""
    frame = _closed_frame(series, drop_partial=drop_partial, now=now)
    if frame.empty:
        raise ValueError(f"no closed candles to chart for {series.symbol} {series.timeframe}")

    window = frame.iloc[-spec.candle_window :]
    ema_series = _ema_overlays(frame, spec, window_len=len(window))
    chart_levels = _levels_for(levels or (features.levels if features else ()), series.timeframe)
    chart_levels = chart_levels[: spec.max_levels]

    png = _draw(window, ema_series, chart_levels, spec, data_quality, degraded_fields)

    params = ChartRenderParams(
        renderer_version=RENDERER_VERSION,
        spec=spec,
        source=series.source,
        fetched_at=series.fetched_at,
        candles_drawn=len(window),
        first_candle_at=window.index[0].to_pydatetime(),
        last_candle_at=window.index[-1].to_pydatetime(),
        last_close=Decimal(str(window["close"].iloc[-1])),
        emas_drawn=tuple(sorted(ema_series)),
        levels_drawn=tuple(
            DrawnLevel(
                price=level.price,
                kind=level.kind.value,
                touches=level.touches,
                timeframe=level.timeframe,
            )
            for level in chart_levels
        ),
        data_quality=data_quality.value,
        degraded_fields=degraded_fields,
        image_sha256=hashlib.sha256(png).hexdigest(),
    )

    log.info(
        "charts.rendered",
        symbol=series.symbol,
        timeframe=series.timeframe,
        candles=len(window),
        levels=len(chart_levels),
        bytes=len(png),
        sha256=params.image_sha256[:12],
    )
    return ChartImage(png=png, params=params)


def render_album(
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    specs: tuple[ChartSpec, ...],
) -> tuple[ChartImage, ...]:
    """The analyst's chart set — one image per timeframe (ARCHITECTURE.md §3)."""
    images: list[ChartImage] = []
    for spec in specs:
        series = snapshot.ohlcv.get(spec.timeframe)
        if series is None:
            log.warning("charts.timeframe_missing", symbol=snapshot.symbol, tf=spec.timeframe)
            continue
        images.append(
            render(
                series,
                spec,
                features=features,
                data_quality=snapshot.data_quality,
                degraded_fields=snapshot.degraded_fields,
                now=snapshot.captured_at,
            )
        )
    return tuple(images)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _closed_frame(series: OHLCVSeries, *, drop_partial: bool, now: datetime | None) -> pd.DataFrame:
    """Closed candles only — same discipline as the feature engine (M2)."""
    from sentinel.features.engine import is_partial

    frame = series.to_frame()
    if drop_partial and len(frame) > 0:
        reference = now if now is not None else series.fetched_at
        last_open = series.candles[-1].open_time
        if is_partial(last_open, series.timeframe, reference):
            frame = frame.iloc[:-1]
    return frame


def _ema_overlays(frame: pd.DataFrame, spec: ChartSpec, *, window_len: int) -> dict[int, pd.Series]:
    """EMAs computed over the full series, then sliced to the drawn window.

    Computed with the same function the feature engine uses, so the line on the
    chart and the number in the JSON can never disagree.
    """
    overlays: dict[int, pd.Series] = {}
    for period in spec.ema_periods:
        if len(frame) < period:
            continue
        values = indicators.ema(frame["close"], period).iloc[-window_len:]
        # A line needs two points. With exactly `period` candles an EMA has a
        # single valid value, which draws nothing — and listing it in the key
        # would announce an EMA that is not on the chart.
        if values.notna().sum() >= 2:
            overlays[period] = values
    return overlays


def _levels_for(levels: tuple[Level, ...], timeframe: str) -> tuple[Level, ...]:
    """Only this timeframe's structure — a cross-timeframe overlay is unreadable."""
    matching = [level for level in levels if level.timeframe == timeframe]
    return tuple(sorted(matching, key=lambda lv: -lv.strength))


def _draw(
    window: pd.DataFrame,
    ema_series: dict[int, pd.Series],
    levels: tuple[Level, ...],
    spec: ChartSpec,
    data_quality: DataQuality,
    degraded_fields: tuple[str, ...],
) -> bytes:
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=mpf.make_marketcolors(**theme.market_colors()),
        facecolor=theme.PANEL,
        figcolor=theme.BACKGROUND,
        edgecolor=theme.GRID,
        gridcolor=theme.GRID,
        gridstyle=":",
        rc=theme.style_rc(),
    )

    # No `label=`: that makes mplfinance draw its own legend inside the axes,
    # where it lands on top of price action. The EMA key goes in the header band.
    addplots = [
        mpf.make_addplot(
            values,
            color=theme.EMA_COLOURS.get(period, theme.MUTED),
            width=1.2,
        )
        for period, values in sorted(ema_series.items())
    ]

    plot_kwargs: dict[str, object] = {
        "type": "candle",
        "style": style,
        "volume": True,
        "figsize": spec.figsize,
        "panel_ratios": (1 - spec.volume_panel_ratio, spec.volume_panel_ratio),
        "returnfig": True,
        "tight_layout": False,
        "xrotation": 0,
        "datetime_format": "%m-%d %H:%M",
        "warn_too_much_data": len(window) + 1,
    }
    # mplfinance rejects `addplot=None` outright — the kwarg has to be absent
    # when there is nothing to overlay (short series, or a small window).
    if addplots:
        plot_kwargs["addplot"] = addplots

    fig, axes = mpf.plot(window, **plot_kwargs)

    _claim_canvas(axes, spec)

    price_ax = axes[0]
    _draw_levels(price_ax, levels, window)
    _draw_watermark(fig, window, spec, data_quality, degraded_fields)
    _draw_ema_key(fig, ema_series)

    buffer = io.BytesIO()
    fig.savefig(
        buffer,
        format="png",
        dpi=spec.dpi,
        facecolor=theme.BACKGROUND,
        metadata=_PNG_METADATA,
    )
    plt.close(fig)
    return buffer.getvalue()


#: Figure-fraction layout. mplfinance hardcodes its panels to 72% x 70% of the
#: canvas, leaving wide dead margins — at this size those are wasted pixels the
#: analyst still pays visual tokens for. Positions are set explicitly instead:
#: reproducible, and it reserves a fixed header band for the watermark.
_LEFT = 0.055  # room for price tick labels
_RIGHT = 0.988
_TOP = 0.905  # below the watermark band
_BOTTOM = 0.062  # room for time tick labels
_PANEL_GAP = 0.022


def _claim_canvas(axes: list[Axes], spec: ChartSpec) -> None:
    """Resize the price and volume panels to fill the figure.

    Each visible panel has an invisible twin for secondary axes; both must move
    together or the twin's ticks drift out of alignment with the data.
    """
    width = _RIGHT - _LEFT
    total_height = _TOP - _BOTTOM - _PANEL_GAP
    volume_height = total_height * spec.volume_panel_ratio
    price_height = total_height - volume_height
    price_bottom = _BOTTOM + volume_height + _PANEL_GAP

    price_box = (_LEFT, price_bottom, width, price_height)
    volume_box = (_LEFT, _BOTTOM, width, volume_height)

    for index, ax in enumerate(axes):
        ax.set_position(price_box if index < 2 else volume_box)


def _format_price(price: float) -> str:
    """Human-readable at any scale. ``{:,.4g}`` renders 64578 as ``6.458e+04``,
    which is unreadable on a chart — precision has to follow magnitude."""
    if price >= 1000:
        return f"{price:,.0f}"
    if price >= 1:
        return f"{price:,.2f}"
    return f"{price:.6g}"


def _draw_levels(ax: Axes, levels: tuple[Level, ...], window: pd.DataFrame) -> None:
    """Horizontal S/R lines, labelled with price and touch count.

    Labels sit on the right, where a trader reads price, and where they cannot
    collide with the EMA key in the top-left.
    """
    if not levels:
        return

    low, high = float(window["low"].min()), float(window["high"].max())
    span = high - low
    for level in levels:
        price = float(level.price)
        # Skip levels outside the drawn price range — an off-chart line would
        # only compress the axis and hide the structure that is visible.
        if not (low - span * 0.05 <= price <= high + span * 0.05):
            continue
        colour = theme.SUPPORT if level.kind is LevelKind.SUPPORT else theme.RESISTANCE
        ax.axhline(price, color=colour, linewidth=1.0, linestyle="--", alpha=0.8)
        ax.text(
            0.997,
            price,
            f"{_format_price(price)}  {level.touches}x",
            transform=ax.get_yaxis_transform(),
            color=theme.LEVEL_LABEL,
            fontsize=9.5,
            va="bottom",
            ha="right",
            bbox={"facecolor": theme.BACKGROUND, "edgecolor": colour, "pad": 1.6, "alpha": 0.85},
        )


def _draw_ema_key(fig: Figure, ema_series: dict[int, pd.Series]) -> None:
    """Colour key for the EMAs, drawn in the header band.

    A matplotlib legend inside the axes would sit on top of price action or the
    level labels depending on the data; the header band is always free.
    """
    if not ema_series:
        return
    for index, period in enumerate(sorted(ema_series)):
        fig.text(
            0.30 + index * 0.075,
            0.962,
            f"— EMA{period}",
            color=theme.EMA_COLOURS.get(period, theme.MUTED),
            fontsize=11,
            fontweight="bold",
            va="top",
            ha="left",
        )


def _draw_watermark(
    fig: Figure,
    window: pd.DataFrame,
    spec: ChartSpec,
    data_quality: DataQuality,
    degraded_fields: tuple[str, ...],
) -> None:
    """Symbol, timeframe, last-closed-candle UTC time, last close, quality flag.

    The timestamp is the last *closed candle*, never wall-clock — that is what
    makes the image reproducible from stored OHLCV.
    """
    last_at = window.index[-1].to_pydatetime()
    last_close = float(window["close"].iloc[-1])

    fig.text(
        0.012,
        0.973,
        f"{spec.symbol}  ·  {spec.timeframe}",
        color=theme.TEXT,
        fontsize=15,
        fontweight="bold",
        va="top",
        ha="left",
    )
    fig.text(
        0.012,
        0.941,
        f"last closed {last_at:%Y-%m-%d %H:%M} UTC   ·   close {last_close:,.6g}",
        color=theme.MUTED,
        fontsize=10,
        va="top",
        ha="left",
    )

    if data_quality is DataQuality.DEGRADED:
        fields = ", ".join(degraded_fields) if degraded_fields else "unspecified"
        fig.text(
            0.988,
            0.973,
            f"⚠ DEGRADED DATA — {fields}",
            color=theme.DEGRADED,
            fontsize=13,
            fontweight="bold",
            va="top",
            ha="right",
            bbox={
                "facecolor": theme.BACKGROUND,
                "edgecolor": theme.DEGRADED,
                "linewidth": 1.6,
                "pad": 3.5,
            },
        )
