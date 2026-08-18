"""Deterministic indicators. Pure functions, no I/O, no LLM (CLAUDE.md).

**Conventions are pinned here on purpose.** Every function states its exact
definition and its warm-up length, and returns ``NaN`` for every bar before the
indicator is defined — an indicator that emits a value before its warm-up has
elapsed is not an indicator, it is noise the analyst would treat as signal.

* ``ema``  — SMA seed at bar ``length-1``, then ``alpha = 2/(length+1)``.
* ``rsi``  — Wilder: average gain/loss seeded with the SMA of the first
  ``length`` deltas, then Wilder smoothing ``(prev*(n-1) + current)/n``.
* ``atr``  — Wilder: true range seeded with the SMA of TR[1..n], same smoothing.

These match the canonical Wilder/TA-Lib definitions. ``pandas-ta``'s pure-python
fallback uses an unseeded EWM that differs at our 200-bar tails and emits values
during warm-up, so it is used only as an independent oracle in the tests.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _as_float(values: pd.Series) -> pd.Series[float]:
    return values.astype("float64")


def sma(values: pd.Series, length: int) -> pd.Series:
    """Simple moving average. Warm-up: ``length - 1`` bars."""
    _validate(values, length)
    return _as_float(values).rolling(window=length, min_periods=length).mean()


def ema(values: pd.Series, length: int) -> pd.Series:
    """Exponential moving average, SMA-seeded.

    ``ema[length-1] = mean(values[0:length])`` then
    ``ema[i] = a*values[i] + (1-a)*ema[i-1]`` with ``a = 2/(length+1)``.
    Warm-up: ``length - 1`` bars.
    """
    _validate(values, length)
    data = _as_float(values).to_numpy()
    out = np.full(data.shape, np.nan)
    if data.size < length:
        return pd.Series(out, index=values.index, name=f"ema{length}")

    alpha = 2.0 / (length + 1.0)
    out[length - 1] = data[:length].mean()
    for i in range(length, data.size):
        out[i] = alpha * data[i] + (1.0 - alpha) * out[i - 1]
    return pd.Series(out, index=values.index, name=f"ema{length}")


def wilder_smooth(values: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing (RMA), seeded with the SMA of the first ``length`` values.

    ``out[length-1] = mean(values[0:length])`` then
    ``out[i] = (out[i-1]*(length-1) + values[i]) / length``.
    """
    _validate(values, length)
    data = _as_float(values).to_numpy()
    out = np.full(data.shape, np.nan)
    if data.size < length:
        return pd.Series(out, index=values.index)

    out[length - 1] = data[:length].mean()
    for i in range(length, data.size):
        out[i] = (out[i - 1] * (length - 1) + data[i]) / length
    return pd.Series(out, index=values.index)


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI. Warm-up: ``length`` bars (first value at index ``length``).

    A series with no down-closes yields 100; no up-closes yields 0.
    """
    _validate(close, length + 1)
    data = _as_float(close).to_numpy()
    delta = np.diff(data)
    gain = np.where(delta > 0.0, delta, 0.0)
    loss = np.where(delta < 0.0, -delta, 0.0)

    n = data.size
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    avg_gain[length] = gain[:length].mean()
    avg_loss[length] = loss[:length].mean()
    for i in range(length + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (length - 1) + gain[i - 1]) / length
        avg_loss[i] = (avg_loss[i - 1] * (length - 1) + loss[i - 1]) / length

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - 100.0 / (1.0 + rs)
    # avg_loss == 0 → RS is infinite → RSI 100; both zero → flat market → 50.
    flat = (avg_loss == 0.0) & (avg_gain == 0.0)
    out = np.where((avg_loss == 0.0) & ~flat, 100.0, out)
    out = np.where(flat, 50.0, out)
    out = np.where(np.isnan(avg_gain), np.nan, out)
    return pd.Series(out, index=close.index, name=f"rsi{length}")


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """``max(h-l, |h-prev_close|, |l-prev_close|)``; the first bar is ``h-l``."""
    _validate(high, 1)
    h, low_, c = _as_float(high), _as_float(low), _as_float(close)
    prev_close = c.shift(1)
    ranges = pd.concat([h - low_, (h - prev_close).abs(), (low_ - prev_close).abs()], axis=1)
    out = ranges.max(axis=1)
    out.iloc[0] = float(h.iloc[0] - low_.iloc[0])
    return pd.Series(out.to_numpy(), index=high.index, name="true_range")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's ATR. Warm-up: ``length`` bars — the first bar's TR is excluded
    from the seed because it has no previous close."""
    _validate(high, length + 1)
    tr = true_range(high, low, close)
    seeded = wilder_smooth(tr.iloc[1:], length)
    values = np.full(len(high), np.nan)
    values[1:] = seeded.to_numpy()
    return pd.Series(values, index=high.index, name=f"atr{length}")


def relative_volume(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Volume vs. the mean of the ``lookback`` **preceding** bars.

    The current bar is excluded from its own baseline, so a volume spike cannot
    dilute the average it is being measured against. Warm-up: ``lookback`` bars.
    """
    _validate(volume, lookback + 1)
    data = _as_float(volume)
    baseline = data.shift(1).rolling(window=lookback, min_periods=lookback).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = data / baseline
    return pd.Series(out.to_numpy(), index=volume.index, name=f"relative_volume{lookback}").replace(
        [np.inf, -np.inf], np.nan
    )


def pct_change(close: pd.Series, periods: int) -> float | None:
    """Percentage change over ``periods`` bars, or None when there is not enough history."""
    if periods <= 0:
        raise ValueError("periods must be positive")
    data = _as_float(close)
    if data.size < periods + 1:
        return None
    previous = float(data.iloc[-1 - periods])
    if previous == 0.0:
        return None
    return (float(data.iloc[-1]) - previous) / previous * 100.0


def percentile_rank(values: pd.Series, value: float) -> float:
    """Share of ``values`` (0-100) that are ``<= value``. Used by the volatility regime."""
    data = _as_float(values).dropna().to_numpy()
    if data.size == 0:
        raise ValueError("percentile_rank needs at least one observation")
    return float((data <= value).sum()) / float(data.size) * 100.0


def _validate(values: pd.Series, minimum: int) -> None:
    if minimum < 1:
        raise ValueError("length must be positive")
    if values.isna().any():
        raise ValueError("indicator inputs must not contain NaN")
