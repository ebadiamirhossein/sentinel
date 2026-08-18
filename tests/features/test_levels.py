"""Pivot detection, clustering and level ranking (the owner-reviewed algorithm)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from sentinel.features.levels import (
    Pivot,
    cluster_pivots,
    detect_levels,
    find_pivots,
    nearest,
)
from sentinel.features.models import LevelKind

START = datetime(2026, 8, 1, tzinfo=UTC)


def frame_from(highs: list[float], lows: list[float] | None = None) -> pd.DataFrame:
    lows = lows if lows is not None else [h - 1.0 for h in highs]
    index = pd.DatetimeIndex([START + timedelta(hours=i) for i in range(len(highs))])
    return pd.DataFrame(
        {
            "open": highs,
            "high": highs,
            "low": lows,
            "close": highs,
            "volume": [1.0] * len(highs),
        },
        index=index,
    )


# ── pivot detection ──────────────────────────────────────────────────────────


def test_detects_a_clear_pivot_high() -> None:
    frame = frame_from([1, 2, 3, 10, 3, 2, 1])

    pivots = find_pivots(frame, window=3)

    highs = [p for p in pivots if p.kind is LevelKind.RESISTANCE]
    assert len(highs) == 1
    assert highs[0].index == 3
    assert highs[0].price == 10.0
    assert highs[0].at == START + timedelta(hours=3)


def test_detects_a_clear_pivot_low() -> None:
    frame = frame_from(highs=[10, 9, 8, 7, 8, 9, 10], lows=[9, 8, 7, 1, 7, 8, 9])

    lows = [p for p in find_pivots(frame, window=3) if p.kind is LevelKind.SUPPORT]

    assert len(lows) == 1
    assert lows[0].index == 3
    assert lows[0].price == 1.0


def test_last_bars_can_never_be_pivots() -> None:
    """No repainting: the tail has no right-side confirmation yet."""
    frame = frame_from([1, 2, 3, 4, 5, 6, 99])

    pivots = find_pivots(frame, window=3)

    assert all(p.index <= len(frame) - 1 - 3 for p in pivots)
    assert not any(p.price == 99.0 for p in pivots)


def test_first_bars_can_never_be_pivots() -> None:
    frame = frame_from([99, 2, 3, 4, 5, 6, 7, 8, 9, 10])

    assert not any(p.price == 99.0 for p in find_pivots(frame, window=3))


def test_monotonic_series_has_no_pivots() -> None:
    frame = frame_from([float(i) for i in range(20)])

    assert [p for p in find_pivots(frame, window=3) if p.kind is LevelKind.RESISTANCE] == []


def test_detection_is_deterministic() -> None:
    frame = frame_from([1, 5, 2, 9, 3, 7, 1, 8, 2, 6, 1, 4, 2])

    assert find_pivots(frame, window=3) == find_pivots(frame, window=3)


def test_window_must_be_positive() -> None:
    with pytest.raises(ValueError, match="window must be positive"):
        find_pivots(frame_from([1, 2, 3]), window=0)


# ── clustering ───────────────────────────────────────────────────────────────


def pivot(price: float, index: int = 0) -> Pivot:
    return Pivot(index, price, START + timedelta(hours=index), LevelKind.RESISTANCE)


def test_nearby_pivots_merge_into_one_level() -> None:
    clusters = cluster_pivots([pivot(100.0, 1), pivot(100.4, 5), pivot(100.8, 9)], tolerance=1.0)

    assert len(clusters) == 1
    assert clusters[0].mean == pytest.approx(100.4)


def test_distant_pivots_stay_separate() -> None:
    clusters = cluster_pivots([pivot(100.0, 1), pivot(120.0, 5)], tolerance=1.0)

    assert len(clusters) == 2


def test_clustering_compares_against_the_running_mean_not_the_last_pivot() -> None:
    """A slowly drifting chain must not swallow the whole tape.

    100.0 and 100.9 merge (mean 100.45); 101.7 is 1.25 from that mean, so it
    starts a new cluster even though it is within tolerance of 100.9 alone.
    """
    clusters = cluster_pivots(
        [pivot(100.0, 1), pivot(100.9, 2), pivot(101.7, 3), pivot(110.0, 4)], tolerance=1.0
    )

    assert [len(c.prices) for c in clusters] == [2, 1, 1]
    assert clusters[0].mean == pytest.approx(100.45)
    assert clusters[-1].mean == 110.0


def test_tolerance_must_be_positive() -> None:
    with pytest.raises(ValueError, match="tolerance must be positive"):
        cluster_pivots([pivot(1.0)], tolerance=0.0)


# ── level construction ───────────────────────────────────────────────────────


BASE = 100.0
GAP = 4  # >= window, so every peak gets left and right confirmation


def peaks_frame(peaks: list[float], *, trailing: int = GAP) -> pd.DataFrame:
    """Isolated single-bar peaks separated by flat base bars.

    Flat lows produce no pivot lows (a plateau has no strict win on either side),
    so these frames test resistances in isolation.
    """
    highs = [BASE] * GAP
    for peak in peaks:
        highs.append(peak)
        highs += [BASE] * GAP
    highs += [BASE] * max(0, trailing - GAP)
    return frame_from(highs, lows=[BASE - 10.0] * len(highs))


def swing_frame() -> pd.DataFrame:
    """Two confirmed touches of 110 resistance and two of 90 support.

    The quiet-bar low (98) is deliberately distinct from the trough (90): if they
    matched, the lows would be flat and no support pivot could form.
    """
    quiet_low = 98.0
    highs = [BASE] * GAP
    lows = [quiet_low] * GAP
    for _ in range(2):
        highs += [110.0, BASE, BASE, BASE, BASE]
        lows += [quiet_low, 90.0, quiet_low, quiet_low, quiet_low]
    highs += [BASE] * GAP
    lows += [quiet_low] * GAP
    return frame_from(highs, lows)


def test_levels_are_classified_against_the_reference_price() -> None:
    frame = swing_frame()

    levels = detect_levels(frame, timeframe="1h", reference_price=100.0, atr=2.0)

    assert levels
    for level in levels:
        if level.kind is LevelKind.RESISTANCE:
            assert level.price > Decimal("100")
        else:
            assert level.price < Decimal("100")


def test_broken_support_flips_to_resistance() -> None:
    """The same zone reclassifies when price moves through it — as traders expect."""
    frame = swing_frame()

    below = detect_levels(frame, timeframe="1h", reference_price=80.0, atr=2.0)
    above = detect_levels(frame, timeframe="1h", reference_price=130.0, atr=2.0)

    assert all(level.kind is LevelKind.RESISTANCE for level in below)
    assert all(level.kind is LevelKind.SUPPORT for level in above)


def test_repeated_touches_raise_strength() -> None:
    frame = peaks_frame([110.0, 110.0, 110.0])

    levels = detect_levels(frame, timeframe="1h", reference_price=BASE, atr=2.0)
    resistance = [lv for lv in levels if lv.kind is LevelKind.RESISTANCE]

    assert resistance[0].price == Decimal("110.0")
    assert resistance[0].touches == 3
    assert resistance[0].strength > Decimal("1.5")


def test_a_multi_touch_old_level_outranks_a_single_touch_fresh_one() -> None:
    """strength = touches x recency_weight, with weight bounded to [0.5, 1.0],
    so age can never fully cancel out repeated confirmation."""
    frame = peaks_frame([110.0, 110.0], trailing=GAP)
    filler = pd.concat([frame] + [frame.iloc[-1:]] * 20)  # long quiet stretch
    filler.index = pd.DatetimeIndex([START + timedelta(hours=i) for i in range(len(filler))])
    fresh = peaks_frame([105.0])
    fresh.index = pd.DatetimeIndex(
        [START + timedelta(hours=len(filler) + i) for i in range(len(fresh))]
    )
    combined = pd.concat([filler, fresh])

    levels = detect_levels(combined, timeframe="1h", reference_price=BASE, atr=1.0)
    resistance = [lv for lv in levels if lv.kind is LevelKind.RESISTANCE]

    assert resistance[0].price == Decimal("110.0")
    assert resistance[0].touches == 2
    assert any(lv.price == Decimal("105.0") for lv in resistance)


def test_max_levels_per_side_is_respected() -> None:
    frame = peaks_frame([110.0, 120.0, 130.0, 140.0, 150.0])

    levels = detect_levels(frame, timeframe="1h", reference_price=BASE, atr=1.0, max_per_side=3)

    assert len([lv for lv in levels if lv.kind is LevelKind.RESISTANCE]) == 3


def test_cluster_width_follows_atr() -> None:
    """A wider ATR merges more retests into one zone."""
    frame = peaks_frame([110.0, 113.0, 116.0])

    tight = detect_levels(frame, timeframe="1h", reference_price=BASE, atr=1.0)
    wide = detect_levels(frame, timeframe="1h", reference_price=BASE, atr=20.0)

    tight_r = [lv for lv in tight if lv.kind is LevelKind.RESISTANCE]
    wide_r = [lv for lv in wide if lv.kind is LevelKind.RESISTANCE]

    assert len(tight_r) == 3  # 0.5 x ATR(1) keeps them apart
    assert len(wide_r) == 1  # 0.5 x ATR(20) merges all three
    assert wide_r[0].touches == 3


def test_distance_pct_is_signed_from_the_reference_price() -> None:
    frame = swing_frame()

    levels = detect_levels(frame, timeframe="1h", reference_price=100.0, atr=2.0)

    for level in levels:
        expected = (level.price - Decimal("100")) / Decimal("100") * 100
        assert level.distance_pct == pytest.approx(expected, abs=Decimal("0.01"))
        assert (level.distance_pct > 0) is (level.kind is LevelKind.RESISTANCE)


def test_zero_atr_yields_no_levels() -> None:
    """No volatility estimate means no defensible cluster width — return nothing."""
    assert detect_levels(swing_frame(), timeframe="1h", reference_price=100.0, atr=0.0) == []


def test_flat_series_yields_no_levels() -> None:
    frame = frame_from([100.0] * 30)

    assert detect_levels(frame, timeframe="1h", reference_price=100.0, atr=1.0) == []


def test_detect_levels_is_deterministic() -> None:
    frame = swing_frame()

    first = detect_levels(frame, timeframe="1h", reference_price=100.0, atr=2.0)
    second = detect_levels(frame, timeframe="1h", reference_price=100.0, atr=2.0)

    assert first == second


# ── nearest ──────────────────────────────────────────────────────────────────


def test_nearest_picks_the_closest_of_a_kind() -> None:
    frame = swing_frame()
    levels = tuple(detect_levels(frame, timeframe="1h", reference_price=100.0, atr=2.0))

    support = nearest(levels, LevelKind.SUPPORT)
    resistance = nearest(levels, LevelKind.RESISTANCE)

    assert support is not None and resistance is not None
    assert support.price < Decimal("100") < resistance.price
    for level in levels:
        if level.kind is LevelKind.SUPPORT:
            assert abs(support.distance_pct) <= abs(level.distance_pct)


def test_nearest_returns_none_when_absent() -> None:
    assert nearest((), LevelKind.SUPPORT) is None
