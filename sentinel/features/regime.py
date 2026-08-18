"""Trend and volatility regime classifiers. Pure, total, no LLM.

**Trend rules (owner-approved 2026-08-18; not defined in any spec).** First match
wins, so the classifier is total — every input maps to exactly one label:

    STRONG_UPTREND    ema20 > ema50 > ema200  and  close > ema20
    UPTREND           ema50 > ema200          and  close > ema50
    STRONG_DOWNTREND  ema20 < ema50 < ema200  and  close < ema20
    DOWNTREND         ema50 < ema200          and  close < ema50
    RANGE             otherwise

When EMA200 is unavailable (e.g. the 1d tail is 100 candles) the same shape is
applied to EMA20/50 alone and the result is marked ``RegimeBasis.REDUCED`` — a
reduced read is never presented as a full one.

**Volatility rules.** ATR% is ranked against its own trailing distribution rather
than compared to fixed cutoffs, because "3% is high" is true for BTC and false
for a small-cap alt: below the 33rd percentile is LOW, above the 67th is HIGH.
"""

from __future__ import annotations

from itertools import pairwise

from sentinel.features.models import RegimeBasis, TrendRegime, VolatilityRegime

BULLISH = frozenset({TrendRegime.UPTREND, TrendRegime.STRONG_UPTREND})
BEARISH = frozenset({TrendRegime.DOWNTREND, TrendRegime.STRONG_DOWNTREND})


def classify_trend(
    close: float | None,
    ema20: float | None,
    ema50: float | None,
    ema200: float | None,
) -> tuple[TrendRegime, RegimeBasis]:
    """Classify one timeframe. Returns the label and which EMAs it was based on."""
    if close is None or ema20 is None or ema50 is None:
        return TrendRegime.UNKNOWN, RegimeBasis.INSUFFICIENT

    if ema200 is None:
        # Reduced basis: same shape, EMA50 stands in for the long-term anchor.
        if ema20 > ema50 and close > ema20:
            return TrendRegime.STRONG_UPTREND, RegimeBasis.REDUCED
        if close > ema50:
            return TrendRegime.UPTREND, RegimeBasis.REDUCED
        if ema20 < ema50 and close < ema20:
            return TrendRegime.STRONG_DOWNTREND, RegimeBasis.REDUCED
        if close < ema50:
            return TrendRegime.DOWNTREND, RegimeBasis.REDUCED
        return TrendRegime.RANGE, RegimeBasis.REDUCED

    if ema20 > ema50 > ema200 and close > ema20:
        return TrendRegime.STRONG_UPTREND, RegimeBasis.FULL
    if ema50 > ema200 and close > ema50:
        return TrendRegime.UPTREND, RegimeBasis.FULL
    if ema20 < ema50 < ema200 and close < ema20:
        return TrendRegime.STRONG_DOWNTREND, RegimeBasis.FULL
    if ema50 < ema200 and close < ema50:
        return TrendRegime.DOWNTREND, RegimeBasis.FULL
    return TrendRegime.RANGE, RegimeBasis.FULL


def classify_volatility(
    atr_pct_percentile: float | None,
    *,
    low_below: float = 33.0,
    high_above: float = 67.0,
) -> VolatilityRegime:
    """Rank-based volatility regime. ``atr_pct_percentile`` is 0-100."""
    if atr_pct_percentile is None:
        return VolatilityRegime.UNKNOWN
    if atr_pct_percentile < low_below:
        return VolatilityRegime.LOW
    if atr_pct_percentile > high_above:
        return VolatilityRegime.HIGH
    return VolatilityRegime.NORMAL


def regimes_aligned(primary: TrendRegime, context: TrendRegime) -> bool | None:
    """True when both timeframes lean the same way; None when either is unknown.

    RANGE never counts as aligned with a trend — that disagreement is exactly the
    signal the analyst needs to see.
    """
    if primary is TrendRegime.UNKNOWN or context is TrendRegime.UNKNOWN:
        return None
    return (
        (primary in BULLISH and context in BULLISH)
        or (primary in BEARISH and context in BEARISH)
        or (primary is TrendRegime.RANGE and context is TrendRegime.RANGE)
    )


def ema_stack_label(ema20: float | None, ema50: float | None, ema200: float | None) -> str | None:
    """Compact EMA ordering flag for the screener block, e.g. ``"20>50>200"``."""
    available = [
        (name, value)
        for name, value in (("20", ema20), ("50", ema50), ("200", ema200))
        if value is not None
    ]
    if len(available) < 2:
        return None

    parts: list[str] = [available[0][0]]
    for (_, previous), (name, current) in pairwise(available):
        parts.append(">" if previous > current else "<" if previous < current else "=")
        parts.append(name)
    return "".join(parts)
