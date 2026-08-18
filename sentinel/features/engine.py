"""Feature engine: MarketSnapshot → SymbolFeatures. Pure computation, no I/O.

Indicators run on **closed candles only**. Binance returns the in-progress bar as
the last row; computing RSI/ATR/relative volume on it would make every value
repaint mid-candle and would read a half-formed bar's volume as a genuine drop.
``MarketSnapshot.last_price`` remains the live price for distance checks.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pandas as pd

from sentinel.core.config import FeaturesConfig
from sentinel.core.logging import get_logger
from sentinel.features import indicators
from sentinel.features import levels as levels_mod
from sentinel.features.models import (
    Level,
    LevelKind,
    RegimeBasis,
    SymbolFeatures,
    TimeframeFeatures,
    TrendRegime,
)
from sentinel.features.regime import (
    classify_trend,
    classify_volatility,
    ema_stack_label,
    regimes_aligned,
)
from sentinel.ingestion.models import MarketSnapshot, OHLCVSeries
from sentinel.ingestion.staleness import timeframe_to_timedelta

log = get_logger(__name__)

#: Bars per timeframe needed for the screener's %change windows (PROMPTS.md §1).
PCT_CHANGE_BARS = {"1h": {"1h": 1, "4h": 4, "24h": 24}, "4h": {"4h": 1, "24h": 6}}


def is_partial(last_open_time: datetime, timeframe: str, now: datetime) -> bool:
    """True when the last candle's period has not closed yet."""
    return last_open_time + timeframe_to_timedelta(timeframe) > now


def to_frame(
    series: OHLCVSeries, *, now: datetime, drop_partial: bool
) -> tuple[pd.DataFrame, bool]:
    """DataFrame of closed candles, plus whether a partial bar was dropped."""
    frame = series.to_frame()
    dropped = False
    if drop_partial and len(frame) > 0:
        last_open = series.candles[-1].open_time
        if is_partial(last_open, series.timeframe, now):
            frame = frame.iloc[:-1]
            dropped = True
    return frame, dropped


def compute_timeframe(
    series: OHLCVSeries,
    config: FeaturesConfig,
    *,
    now: datetime,
) -> TimeframeFeatures | None:
    """Indicators + regimes for one timeframe. None when there are no closed candles."""
    frame, dropped = to_frame(series, now=now, drop_partial=config.drop_partial_candle)
    if frame.empty:
        return None

    close, high, low, volume = frame["close"], frame["high"], frame["low"], frame["volume"]

    ema20 = _last(indicators.ema(close, 20)) if len(frame) >= 20 else None
    ema50 = _last(indicators.ema(close, 50)) if len(frame) >= 50 else None
    ema200 = _last(indicators.ema(close, 200)) if len(frame) >= 200 else None

    rsi14 = (
        _last(indicators.rsi(close, config.rsi_period))
        if len(frame) >= config.rsi_period + 1
        else None
    )

    atr_series = (
        indicators.atr(high, low, close, config.atr_period)
        if len(frame) >= config.atr_period + 1
        else None
    )
    atr14 = _last(atr_series) if atr_series is not None else None

    last_close = float(close.iloc[-1])
    atr_pct = (atr14 / last_close * 100.0) if atr14 is not None and last_close else None

    atr_pct_percentile: float | None = None
    if atr_series is not None and atr_pct is not None:
        pct_series = (atr_series / close * 100.0).dropna()
        window = pct_series.iloc[-config.volatility_lookback :]
        if len(window) >= config.volatility_min_observations:
            atr_pct_percentile = indicators.percentile_rank(window, atr_pct)

    rel_volume = (
        _last(indicators.relative_volume(volume, config.relative_volume_lookback))
        if len(frame) >= config.relative_volume_lookback + 1
        else None
    )

    trend, basis = classify_trend(last_close, ema20, ema50, ema200)
    if basis is RegimeBasis.FULL and len(frame) < 200:
        basis = RegimeBasis.REDUCED

    return TimeframeFeatures(
        timeframe=series.timeframe,
        candles_used=len(frame),
        last_close=_dec(last_close),
        last_closed_at=frame.index[-1].to_pydatetime(),
        partial_candle_dropped=dropped,
        ema20=_dec_opt(ema20),
        ema50=_dec_opt(ema50),
        ema200=_dec_opt(ema200),
        rsi14=_dec_opt(rsi14),
        atr14=_dec_opt(atr14),
        atr_pct=_dec_opt(atr_pct),
        relative_volume=_dec_opt(rel_volume),
        pct_change_1=_dec_opt(indicators.pct_change(close, 1)),
        trend_regime=trend,
        regime_basis=basis,
        volatility_regime=classify_volatility(
            atr_pct_percentile,
            low_below=config.volatility_low_percentile,
            high_above=config.volatility_high_percentile,
        ),
        atr_pct_percentile=_dec_opt(atr_pct_percentile),
        price_above_ema20=None if ema20 is None else last_close > ema20,
        price_above_ema50=None if ema50 is None else last_close > ema50,
        price_above_ema200=None if ema200 is None else last_close > ema200,
        ema_stack=ema_stack_label(ema20, ema50, ema200),
    )


def compute(snapshot: MarketSnapshot, config: FeaturesConfig) -> SymbolFeatures:
    """Compute every feature for one snapshot."""
    now = snapshot.captured_at
    timeframes: dict[str, TimeframeFeatures] = {}
    for timeframe, series in snapshot.ohlcv.items():
        computed = compute_timeframe(series, config, now=now)
        if computed is not None:
            timeframes[timeframe] = computed

    reference_price = snapshot.last_price
    detected: list[Level] = []
    for timeframe in config.level_timeframes:
        level_series = snapshot.ohlcv.get(timeframe)
        level_features = timeframes.get(timeframe)
        if level_series is None or level_features is None or level_features.atr14 is None:
            continue
        frame, _ = to_frame(level_series, now=now, drop_partial=config.drop_partial_candle)
        detected.extend(
            levels_mod.detect_levels(
                frame,
                timeframe=timeframe,
                reference_price=float(reference_price),
                atr=float(level_features.atr14),
                window=config.pivot_window,
                cluster_atr_multiple=config.level_cluster_atr_multiple,
                max_per_side=config.max_levels_per_side,
            )
        )

    all_levels = tuple(detected)
    nearest_support = levels_mod.nearest(all_levels, LevelKind.SUPPORT)
    nearest_resistance = levels_mod.nearest(all_levels, LevelKind.RESISTANCE)

    primary = timeframes.get(config.primary_timeframe)
    context = timeframes.get(config.context_timeframe)
    htf_regime = context.trend_regime if context else TrendRegime.UNKNOWN

    symbol_features = SymbolFeatures(
        symbol=snapshot.symbol,
        computed_at=now,
        reference_price=reference_price,
        timeframes=timeframes,
        levels=all_levels,
        htf_regime=htf_regime,
        regime_aligned=(regimes_aligned(primary.trend_regime, htf_regime) if primary else None),
        nearest_support=nearest_support.price if nearest_support else None,
        nearest_resistance=nearest_resistance.price if nearest_resistance else None,
        distance_to_support_pct=(nearest_support.distance_pct if nearest_support else None),
        distance_to_resistance_pct=(
            nearest_resistance.distance_pct if nearest_resistance else None
        ),
        pct_change_1h=_change(snapshot, "1h", 1),
        pct_change_4h=_change(snapshot, "1h", 4),
        pct_change_24h=_change(snapshot, "1h", 24),
    )

    log.info(
        "features.computed",
        symbol=snapshot.symbol,
        timeframes=sorted(timeframes),
        levels=len(all_levels),
        htf_regime=htf_regime.value,
        regime_aligned=symbol_features.regime_aligned,
    )
    return symbol_features


def attach(snapshot: MarketSnapshot, features: SymbolFeatures) -> MarketSnapshot:
    """Return a copy of the snapshot carrying the serialized feature block.

    ``MarketSnapshot.features`` stays ``dict[str, Any]`` so ``ingestion`` never
    imports ``features`` (no cycle) and the JSONB column stores it as-is.
    """
    return snapshot.model_copy(update={"features": features.model_dump(mode="json")})


def _change(snapshot: MarketSnapshot, timeframe: str, bars: int) -> Decimal | None:
    series = snapshot.ohlcv.get(timeframe)
    if series is None:
        return None
    closes = pd.Series([float(candle.close) for candle in series.candles])
    return _dec_opt(indicators.pct_change(closes, bars))


def _last(series: pd.Series) -> float | None:
    value = series.dropna()
    return None if value.empty else float(value.iloc[-1])


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def _dec_opt(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


__all__ = ["attach", "compute", "compute_timeframe", "is_partial", "to_frame"]
