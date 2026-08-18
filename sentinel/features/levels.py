"""Pivot-based support/resistance detection (owner-approved 2026-08-18).

Algorithm — **confirmed fractal pivots, clustered into ATR-wide bands**:

1. **Detect.** Bar ``i`` is a pivot high when ``high[i] == max(high[i-k … i+k])``
   and strictly exceeds at least one neighbour on each side, so a flat plateau
   does not register as several pivots. Mirrored for pivot lows. Default ``k=3``.
2. **Confirm.** The last ``k`` bars cannot be pivots — they have no right-hand
   confirmation yet. Nothing repaints: the same OHLCV always yields the same
   levels, which the chart renderer (M3) and the backtest harness both need.
3. **Cluster.** Pivots are merged while they stay within ``tol = 0.5 x ATR(14)``
   of the running cluster mean. Volatility-adaptive rather than a fixed percent.
4. **Score.** ``strength = touches x recency_weight`` where
   ``recency_weight = 0.5 + 0.5 x (1 - bars_since_last_touch / lookback)``, so a
   3-touch old level (1.5) still outranks a 1-touch fresh one (1.0).
5. **Classify.** Below the reference price → SUPPORT, above → RESISTANCE. Broken
   support therefore flips to resistance on its own.
6. **Select.** Top N per side by strength, ties broken by proximity to price.

Pure functions: no I/O, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pandas as pd

from sentinel.features.models import Level, LevelKind


@dataclass(frozen=True)
class Pivot:
    index: int
    price: float
    at: datetime
    kind: LevelKind


@dataclass
class _Cluster:
    prices: list[float]
    indices: list[int]
    times: list[datetime]

    @property
    def mean(self) -> float:
        return sum(self.prices) / len(self.prices)


def find_pivots(frame: pd.DataFrame, window: int = 3) -> list[Pivot]:
    """Confirmed fractal pivots. ``frame`` needs high/low columns and a datetime index."""
    if window < 1:
        raise ValueError("window must be positive")

    highs = frame["high"].astype("float64").to_numpy()
    lows = frame["low"].astype("float64").to_numpy()
    times = list(frame.index)
    pivots: list[Pivot] = []

    # Range stops `window` bars early: the tail has no right-side confirmation.
    for i in range(window, len(highs) - window):
        left_h = highs[i - window : i]
        right_h = highs[i + 1 : i + window + 1]
        # The second clause rejects plateaus: require a strict win on one side.
        if (
            highs[i] >= left_h.max()
            and highs[i] >= right_h.max()
            and (highs[i] > left_h.min() or highs[i] > right_h.min())
        ):
            pivots.append(Pivot(i, float(highs[i]), times[i], LevelKind.RESISTANCE))

        left_l = lows[i - window : i]
        right_l = lows[i + 1 : i + window + 1]
        if (
            lows[i] <= left_l.min()
            and lows[i] <= right_l.min()
            and (lows[i] < left_l.max() or lows[i] < right_l.max())
        ):
            pivots.append(Pivot(i, float(lows[i]), times[i], LevelKind.SUPPORT))

    return pivots


def cluster_pivots(pivots: list[Pivot], tolerance: float) -> list[_Cluster]:
    """Merge pivots whose price sits within ``tolerance`` of the running cluster mean."""
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")

    clusters: list[_Cluster] = []
    for pivot in sorted(pivots, key=lambda p: p.price):
        if clusters and abs(pivot.price - clusters[-1].mean) <= tolerance:
            current = clusters[-1]
            current.prices.append(pivot.price)
            current.indices.append(pivot.index)
            current.times.append(pivot.at)
        else:
            clusters.append(_Cluster([pivot.price], [pivot.index], [pivot.at]))
    return clusters


def detect_levels(
    frame: pd.DataFrame,
    *,
    timeframe: str,
    reference_price: float,
    atr: float,
    window: int = 3,
    cluster_atr_multiple: float = 0.5,
    max_per_side: int = 3,
) -> list[Level]:
    """Full pipeline: pivots → clusters → scored, classified, ranked levels."""
    if atr <= 0:
        return []

    pivots = find_pivots(frame, window=window)
    if not pivots:
        return []

    lookback = max(len(frame) - 1, 1)
    last_index = len(frame) - 1
    clusters = cluster_pivots(pivots, tolerance=atr * cluster_atr_multiple)

    levels: list[Level] = []
    for cluster in clusters:
        price = cluster.mean
        touches = len(cluster.prices)
        bars_since = last_index - max(cluster.indices)
        recency_weight = 0.5 + 0.5 * (1.0 - min(bars_since / lookback, 1.0))
        kind = LevelKind.SUPPORT if price < reference_price else LevelKind.RESISTANCE

        levels.append(
            Level(
                price=_dec(price),
                kind=kind,
                timeframe=timeframe,
                touches=touches,
                strength=_dec(touches * recency_weight),
                first_touch_at=min(cluster.times),
                last_touch_at=max(cluster.times),
                distance_pct=_dec((price - reference_price) / reference_price * 100.0),
            )
        )

    return _rank(levels, max_per_side=max_per_side)


def _rank(levels: list[Level], *, max_per_side: int) -> list[Level]:
    """Strongest first, ties broken by proximity; capped per side."""

    def key(level: Level) -> tuple[Decimal, Decimal]:
        return (-level.strength, abs(level.distance_pct))

    supports = sorted((lv for lv in levels if lv.kind is LevelKind.SUPPORT), key=key)
    resistances = sorted((lv for lv in levels if lv.kind is LevelKind.RESISTANCE), key=key)
    return supports[:max_per_side] + resistances[:max_per_side]


def nearest(levels: tuple[Level, ...], kind: LevelKind) -> Level | None:
    """Closest level of a kind to the reference price, by absolute distance."""
    candidates = [level for level in levels if level.kind is kind]
    if not candidates:
        return None
    return min(candidates, key=lambda level: abs(level.distance_pct))


def _dec(value: float) -> Decimal:
    return Decimal(str(value))
