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
from collections.abc import Callable
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
    AnnotationKind,
    ChartAnnotation,
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
    annotations: tuple[ChartAnnotation, ...] = (),
    data_quality: DataQuality = DataQuality.OK,
    degraded_fields: tuple[str, ...] = (),
    drop_partial: bool = True,
    now: datetime | None = None,
) -> ChartImage:
    """Render one timeframe. ``now`` is only used to identify a partial candle.

    ``annotations`` are the market-structure marks forex adds (FOREX.md §6) — prior
    day and week levels, the daily and weekly open, session shading. Crypto passes
    none and its bytes do not move. Whether a **volume panel** is drawn is decided
    from the data, never from a field on ``spec``: a new ``ChartSpec`` field would
    appear in every params record including crypto's, which the golden pins.
    """
    frame = _closed_frame(series, drop_partial=drop_partial, now=now)
    if frame.empty:
        raise ValueError(f"no closed candles to chart for {series.symbol} {series.timeframe}")

    window = frame.iloc[-spec.candle_window :]
    ema_series = _ema_overlays(frame, spec, window_len=len(window))
    chart_levels = _levels_for(levels or (features.levels if features else ()), series.timeframe)
    chart_levels = chart_levels[: spec.max_levels]
    drawn_annotations = _annotations_in_view(annotations, window)

    png = _draw(
        window, ema_series, chart_levels, drawn_annotations, spec, data_quality, degraded_fields
    )

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
        annotations=drawn_annotations,
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
        annotations=len(drawn_annotations),
        volume_panel=_has_volume(window),
        bytes=len(png),
        sha256=params.image_sha256[:12],
    )
    return ChartImage(png=png, params=params)


def render_album(
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    specs: tuple[ChartSpec, ...],
    *,
    annotations: tuple[ChartAnnotation, ...] = (),
) -> tuple[ChartImage, ...]:
    """The analyst's chart set — one image per timeframe (ARCHITECTURE.md §3).

    One annotation set for the whole album: a prior-day high is the same price on
    every timeframe, and each render clips the bands to its own window.
    """
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
                annotations=annotations,
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


def _has_volume(window: pd.DataFrame) -> bool:
    """Does this market have volume at all? Read from the data, never configured.

    ``OHLCVSeries.to_frame`` puts NaN — never 0.0 — in the volume column when the
    candles carry none, which is exactly so this question has an answer here
    (FOREX.md §2.1). Forex has no volume of any kind: no field, no tick count, nothing.

    Deciding it from a ``ChartSpec`` flag instead would put the flag in every params
    record, crypto's included, and move a golden for a market that does not have the
    concept. Recorded in journal/M10b_REPORT.md §12 as a constraint on this milestone.
    """
    return bool(window["volume"].notna().any())


def _draw(
    window: pd.DataFrame,
    ema_series: dict[int, pd.Series],
    levels: tuple[Level, ...],
    annotations: tuple[ChartAnnotation, ...],
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

    has_volume = _has_volume(window)
    plot_kwargs: dict[str, object] = {
        "type": "candle",
        "style": style,
        "figsize": spec.figsize,
        "returnfig": True,
        "tight_layout": False,
        "xrotation": 0,
        "datetime_format": "%m-%d %H:%M",
        "warn_too_much_data": len(window) + 1,
    }
    # No volume panel **at all** rather than an empty one, when the market has none.
    # The kwargs have to be absent rather than False-valued for the panel geometry
    # below to line up: with volume, mplfinance returns four axes (two visible panels
    # and their invisible twins); without it, two.
    if has_volume:
        plot_kwargs["volume"] = True
        plot_kwargs["panel_ratios"] = (1 - spec.volume_panel_ratio, spec.volume_panel_ratio)

    # mplfinance rejects `addplot=None` outright — the kwarg has to be absent
    # when there is nothing to overlay (short series, or a small window).
    if addplots:
        plot_kwargs["addplot"] = addplots

    fig, axes = mpf.plot(window, **plot_kwargs)

    _claim_canvas(axes, spec, has_volume=has_volume)

    price_ax = axes[0]
    # Bands first: they are context and must sit behind everything.
    _draw_session_bands(price_ax, annotations, window)
    _draw_reference_lines(price_ax, annotations, window)
    # Level labels at this instrument's precision. `_format_price` rounds anything
    # above 1 to two decimals, which is right for a crypto S/R label and useless on a
    # pair quoted to five: every level on a EURUSD chart would read "1.17".
    #
    # The branch is `has_volume` — the same data-derived signal the panel is decided
    # from, and the only one the renderer has. It is not really about volume; it is
    # that a market with no volume is a forex one, and this is where the renderer
    # learns that from the data rather than from a spec field it is forbidden to add.
    # `_format_price` itself is untouched: it is on the crypto path, and changing it
    # would move the bytes of every crypto chart quoted between 1 and 1000 — which
    # the golden (BTCUSDT, above 1000) would not even have caught.
    _draw_levels(
        price_ax,
        levels,
        window,
        format_price=_format_price if has_volume else _format_reference_price,
    )
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


def _claim_canvas(axes: list[Axes], spec: ChartSpec, *, has_volume: bool) -> None:
    """Resize the price and volume panels to fill the figure.

    Each visible panel has an invisible twin for secondary axes; both must move
    together or the twin's ticks drift out of alignment with the data.

    With no volume panel there is one visible panel and its twin, and the price box
    claims the whole canvas. The two-panel branch below is untouched, so crypto's
    geometry — and therefore its bytes — cannot move.
    """
    width = _RIGHT - _LEFT
    if not has_volume:
        full = (_LEFT, _BOTTOM, width, _TOP - _BOTTOM)
        for ax in axes:
            ax.set_position(full)
        return

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


def _draw_levels(
    ax: Axes,
    levels: tuple[Level, ...],
    window: pd.DataFrame,
    *,
    format_price: Callable[[float], str] = _format_price,
) -> None:
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
            f"{format_price(price)}  {level.touches}x",
            transform=ax.get_yaxis_transform(),
            color=theme.LEVEL_LABEL,
            fontsize=9.5,
            va="bottom",
            ha="right",
            bbox={"facecolor": theme.BACKGROUND, "edgecolor": colour, "pad": 1.6, "alpha": 0.85},
        )


#: How far outside the drawn price range a reference line may sit and still be worth
#: drawing. Same tolerance the S/R lines use — beyond it the line only compresses the
#: axis and hides the structure that is actually visible.
_PRICE_MARGIN = 0.05

#: Reference lines that mark a period's *open* read differently from ones that mark a
#: high or a low, so they get their own colour.
_OPEN_KINDS = frozenset({AnnotationKind.DAILY_OPEN, AnnotationKind.WEEKLY_OPEN})


def _price_bounds(window: pd.DataFrame) -> tuple[float, float]:
    low, high = float(window["low"].min()), float(window["high"].max())
    span = high - low
    return low - span * _PRICE_MARGIN, high + span * _PRICE_MARGIN


def _annotations_in_view(
    annotations: tuple[ChartAnnotation, ...], window: pd.DataFrame
) -> tuple[ChartAnnotation, ...]:
    """The subset that will actually be drawn on *this* window.

    Filtering here rather than inside the drawing functions is what lets the params
    record what was drawn instead of what was asked for. A record that listed an
    off-chart line would be a record of an image that does not exist.
    """
    if not annotations:
        return ()
    lowest, highest = _price_bounds(window)
    first = window.index[0].to_pydatetime()
    last = window.index[-1].to_pydatetime()

    kept: list[ChartAnnotation] = []
    for annotation in annotations:
        if annotation.kind.is_band:
            assert annotation.from_at is not None and annotation.to_at is not None
            if annotation.from_at <= last and annotation.to_at > first:
                kept.append(annotation)
        else:
            assert annotation.price is not None
            if lowest <= float(annotation.price) <= highest:
                kept.append(annotation)
    return tuple(kept)


def _draw_session_bands(
    ax: Axes, annotations: tuple[ChartAnnotation, ...], window: pd.DataFrame
) -> None:
    """Shade the session bands behind the candles.

    mplfinance draws on a *positional* x-axis, so a band's timestamps have to be
    mapped back to bar indices — there is no datetime to hand to ``axvspan``. Bars are
    half a slot wide either side of their centre, which is how the shading lines up
    with the candle edges rather than their midpoints.
    """
    bands = [a for a in annotations if a.kind.is_band]
    if not bands:
        return
    stamps = [ts.to_pydatetime() for ts in window.index]
    for band in bands:
        assert band.from_at is not None and band.to_at is not None
        inside = [i for i, ts in enumerate(stamps) if band.from_at <= ts < band.to_at]
        if not inside:
            continue
        ax.axvspan(
            inside[0] - 0.5,
            inside[-1] + 0.5,
            color=theme.SESSION_BAND,
            alpha=theme.SESSION_BAND_ALPHA,
            linewidth=0,
            zorder=0,
        )


def _format_reference_price(price: float) -> str:
    """Reference-line prices, at forex precision.

    Deliberately **not** :func:`_format_price`, which rounds anything above 1 to two
    decimals — that is right for a crypto S/R label and useless here, where a prior-day
    high of 1.1725 and a prior-day low of 1.1655 would both read "1.17". Six
    significant figures covers 1.1725 and 150.25 alike, and it is the same rule the
    watermark's close already uses. ``_format_price`` is left exactly as it was, because
    it is on the crypto path and the golden pins its output.
    """
    return f"{price:,.6g}"


def _draw_reference_lines(
    ax: Axes, annotations: tuple[ChartAnnotation, ...], window: pd.DataFrame
) -> None:
    """Prior day/week levels and the period opens (FOREX.md §6).

    Dotted, and labelled on the **left** — the S/R lines are dashed and labelled on
    the right, so the two kinds of claim stay visually distinct and their labels
    cannot collide. They are different claims: an S/R level was derived from touches,
    a prior-day high is simply where yesterday ended up.
    """
    lines = [a for a in annotations if not a.kind.is_band]
    if not lines:
        return
    for line in lines:
        assert line.price is not None
        price = float(line.price)
        colour = theme.REFERENCE_OPEN if line.kind in _OPEN_KINDS else theme.REFERENCE
        ax.axhline(price, color=colour, linewidth=1.0, linestyle=":", alpha=0.85)
        ax.text(
            0.003,
            price,
            f"{line.label}  {_format_reference_price(price)}",
            transform=ax.get_yaxis_transform(),
            color=theme.LEVEL_LABEL,
            fontsize=9.0,
            va="bottom",
            ha="left",
            bbox={"facecolor": theme.BACKGROUND, "edgecolor": colour, "pad": 1.4, "alpha": 0.85},
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
