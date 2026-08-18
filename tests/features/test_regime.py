"""Regime classifiers — explicit truth tables.

The trend rules are owner-approved and defined nowhere in the specs, so they are
pinned here case by case: if someone changes a comparison, one of these fails.
"""

from __future__ import annotations

import pytest

from sentinel.features.models import RegimeBasis, TrendRegime, VolatilityRegime
from sentinel.features.regime import (
    classify_trend,
    classify_volatility,
    ema_stack_label,
    regimes_aligned,
)

# close, ema20, ema50, ema200 → regime, basis
FULL_BASIS_TRUTH_TABLE = [
    # ── stacked bullish ──────────────────────────────────────────────────────
    ("above whole stack", 105.0, 104.0, 103.0, 102.0, TrendRegime.STRONG_UPTREND),
    ("stacked, price under ema20", 103.5, 104.0, 103.0, 102.0, TrendRegime.UPTREND),
    ("stacked, price under ema50", 102.5, 104.0, 103.0, 102.0, TrendRegime.RANGE),
    ("stacked, price under ema200", 101.0, 104.0, 103.0, 102.0, TrendRegime.RANGE),
    # ── stacked bearish ──────────────────────────────────────────────────────
    ("below whole stack", 95.0, 96.0, 97.0, 98.0, TrendRegime.STRONG_DOWNTREND),
    ("stacked down, price over ema20", 96.5, 96.0, 97.0, 98.0, TrendRegime.DOWNTREND),
    ("stacked down, price over ema50", 97.5, 96.0, 97.0, 98.0, TrendRegime.RANGE),
    ("stacked down, price over ema200", 99.0, 96.0, 97.0, 98.0, TrendRegime.RANGE),
    # ── crossed / transitional stacks ────────────────────────────────────────
    ("ema20 below ema50, price over ema50", 107.0, 104.0, 106.0, 102.0, TrendRegime.UPTREND),
    ("ema20 below ema50, price under ema50", 105.0, 104.0, 106.0, 102.0, TrendRegime.RANGE),
    # EMA20 curling up does not rescue a downtrend: rule 4 only asks about
    # ema50 vs ema200 and price vs ema50.
    ("ema20 over ema50, price under both", 97.0, 99.0, 98.0, 100.0, TrendRegime.DOWNTREND),
    ("golden-cross pending", 99.5, 100.0, 99.0, 101.0, TrendRegime.RANGE),
    # ── degenerate ───────────────────────────────────────────────────────────
    ("all equal", 100.0, 100.0, 100.0, 100.0, TrendRegime.RANGE),
    ("emas equal, price above", 101.0, 100.0, 100.0, 100.0, TrendRegime.RANGE),
    ("emas equal, price below", 99.0, 100.0, 100.0, 100.0, TrendRegime.RANGE),
]


@pytest.mark.parametrize(
    ("label", "close", "ema20", "ema50", "ema200", "expected"), FULL_BASIS_TRUTH_TABLE
)
def test_trend_truth_table_full_basis(
    label: str, close: float, ema20: float, ema50: float, ema200: float, expected: TrendRegime
) -> None:
    regime, basis = classify_trend(close, ema20, ema50, ema200)

    assert regime is expected, label
    assert basis is RegimeBasis.FULL


REDUCED_BASIS_TRUTH_TABLE = [
    ("above both", 105.0, 104.0, 103.0, TrendRegime.STRONG_UPTREND),
    ("between emas, above ema50", 103.5, 104.0, 103.0, TrendRegime.UPTREND),
    ("below both, stacked down", 95.0, 96.0, 97.0, TrendRegime.STRONG_DOWNTREND),
    ("between emas, below ema50", 96.5, 96.0, 97.0, TrendRegime.DOWNTREND),
    ("equal emas, price equal", 100.0, 100.0, 100.0, TrendRegime.RANGE),
]


@pytest.mark.parametrize(
    ("label", "close", "ema20", "ema50", "expected"), REDUCED_BASIS_TRUTH_TABLE
)
def test_trend_truth_table_reduced_basis(
    label: str, close: float, ema20: float, ema50: float, expected: TrendRegime
) -> None:
    """The 1d tail is 100 candles, so EMA200 is absent — say so, don't fake it."""
    regime, basis = classify_trend(close, ema20, ema50, None)

    assert regime is expected, label
    assert basis is RegimeBasis.REDUCED


@pytest.mark.parametrize(
    ("close", "ema20", "ema50"),
    [(None, 1.0, 1.0), (1.0, None, 1.0), (1.0, 1.0, None), (None, None, None)],
)
def test_missing_inputs_are_unknown_not_range(
    close: float | None, ema20: float | None, ema50: float | None
) -> None:
    """UNKNOWN and RANGE mean different things; conflating them would mislead."""
    regime, basis = classify_trend(close, ema20, ema50, 1.0)

    assert regime is TrendRegime.UNKNOWN
    assert basis is RegimeBasis.INSUFFICIENT


def test_classifier_is_total() -> None:
    """Every combination lands on exactly one label, and nothing raises."""
    values = [98.0, 99.0, 100.0, 101.0, 102.0]
    seen: set[TrendRegime] = set()

    for close in values:
        for ema20 in values:
            for ema50 in values:
                for ema200 in [*values, None]:
                    regime, basis = classify_trend(close, ema20, ema50, ema200)
                    assert isinstance(regime, TrendRegime)
                    assert regime is not TrendRegime.UNKNOWN
                    assert basis is (RegimeBasis.REDUCED if ema200 is None else RegimeBasis.FULL)
                    seen.add(regime)

    assert seen == {
        TrendRegime.STRONG_UPTREND,
        TrendRegime.UPTREND,
        TrendRegime.RANGE,
        TrendRegime.DOWNTREND,
        TrendRegime.STRONG_DOWNTREND,
    }


# ── volatility ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("percentile", "expected"),
    [
        (0.0, VolatilityRegime.LOW),
        (32.9, VolatilityRegime.LOW),
        (33.0, VolatilityRegime.NORMAL),  # boundary is inclusive of NORMAL
        (50.0, VolatilityRegime.NORMAL),
        (67.0, VolatilityRegime.NORMAL),
        (67.1, VolatilityRegime.HIGH),
        (100.0, VolatilityRegime.HIGH),
        (None, VolatilityRegime.UNKNOWN),
    ],
)
def test_volatility_truth_table(percentile: float | None, expected: VolatilityRegime) -> None:
    assert classify_volatility(percentile) is expected


def test_volatility_thresholds_are_configurable() -> None:
    assert classify_volatility(40.0, low_below=50.0, high_above=90.0) is VolatilityRegime.LOW


# ── alignment ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("primary", "context", "expected"),
    [
        (TrendRegime.UPTREND, TrendRegime.STRONG_UPTREND, True),
        (TrendRegime.STRONG_UPTREND, TrendRegime.UPTREND, True),
        (TrendRegime.DOWNTREND, TrendRegime.STRONG_DOWNTREND, True),
        (TrendRegime.RANGE, TrendRegime.RANGE, True),
        (TrendRegime.UPTREND, TrendRegime.DOWNTREND, False),
        (TrendRegime.UPTREND, TrendRegime.RANGE, False),
        (TrendRegime.RANGE, TrendRegime.UPTREND, False),
        (TrendRegime.UNKNOWN, TrendRegime.UPTREND, None),
        (TrendRegime.UPTREND, TrendRegime.UNKNOWN, None),
    ],
)
def test_alignment_truth_table(
    primary: TrendRegime, context: TrendRegime, expected: bool | None
) -> None:
    assert regimes_aligned(primary, context) is expected


# ── EMA stack label ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ema20", "ema50", "ema200", "expected"),
    [
        (104.0, 103.0, 102.0, "20>50>200"),
        (96.0, 97.0, 98.0, "20<50<200"),
        (104.0, 103.0, None, "20>50"),
        (100.0, 100.0, 100.0, "20=50=200"),
        (104.0, 102.0, 103.0, "20>50<200"),
        (None, None, None, None),
        (104.0, None, None, None),
    ],
)
def test_ema_stack_label(
    ema20: float | None, ema50: float | None, ema200: float | None, expected: str | None
) -> None:
    assert ema_stack_label(ema20, ema50, ema200) == expected
