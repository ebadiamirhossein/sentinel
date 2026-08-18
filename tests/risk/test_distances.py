"""Distance percentages on the plan (owner directive 2026-08-18, from M6).

The signal card must be able to say ``TP1: 85.20 (+3.0541%)`` and
``1) 83.10 (-0.3597%)`` without the bot doing arithmetic. A number the card needs
and the plan lacks is a gap in the risk engine, not a line to delete from the
card — so these are engine outputs, hand-calculated here first.

Two different reference prices, deliberately:

* target distance is measured from ``avg_entry`` — §3's weighted average entry,
  the same basis as ``stop_distance_pct`` and every RR figure on the plan, so the
  three reconcile with each other;
* rung distance is measured from ``last_price`` — "how far below the current
  price does my ladder sit", which is what the owner is asking at 3am.

And two different sign conventions, also deliberately: see
``test_a_rung_above_the_last_price_is_positive``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.analyst.models import AnalystReport, Direction
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import GateStatus, TradePlan
from sentinel.risk.rounding import percent
from sentinel.risk.sizing import distance_pct

from .conftest import account, market, portfolio, report


def plan_for(
    config: AppConfig,
    clock: FrozenClock,
    rep: AnalystReport,
    *,
    last_price: str = "83.40",
    capital: str = "10000",
) -> TradePlan:
    decision = RiskEngine(config, clock=clock).evaluate(
        report=rep,
        market=market(last_price=last_price),
        account=account(capital_eur=capital),
        portfolio=portfolio(),
    )
    assert decision.status is GateStatus.APPROVED_FOR_HUMAN, decision.message
    assert decision.plan is not None
    return decision.plan


# ── the pure helper ──────────────────────────────────────────────────────── #


def test_distance_pct_is_a_signed_percentage_of_the_reference() -> None:
    """(83.10 - 83.40) / 83.40 = -0.00359712... -> -0.3597%."""
    assert distance_pct(Decimal("83.40"), Decimal("83.10"), signed=True) == Decimal("-0.36")


def test_distance_pct_unsigned_returns_the_magnitude() -> None:
    """The same pair, as a magnitude — what a target distance reports."""
    assert distance_pct(Decimal("83.40"), Decimal("83.10"), signed=False) == Decimal("0.36")


def test_distance_pct_of_the_reference_itself_is_zero() -> None:
    assert distance_pct(Decimal("83.40"), Decimal("83.40"), signed=True) == Decimal("0.0000")


def test_distance_pct_quantizes_to_four_places_like_every_other_percentage() -> None:
    """Same ``percent()`` as ``stop_distance_pct``: a card must not mix precisions."""
    value = distance_pct(Decimal("82.675"), Decimal("85.20"), signed=False)
    assert value == percent(value)
    assert value == Decimal("3.05")


def test_distance_pct_rejects_a_non_positive_reference() -> None:
    """A zero reference price is not a small number, it is missing data."""
    with pytest.raises(ValueError, match="positive reference"):
        distance_pct(Decimal(0), Decimal("83.10"), signed=True)


# ── the long baseline, every figure calculated by hand ───────────────────── #
#
# avg_entry 82.675, last_price 83.40, targets 85.20 / 86.60 / 88.90.
#
#   rung 1  (83.10 - 83.40) / 83.40 = -0.30 / 83.40 = -0.00359712  -> -0.3597%
#   rung 2  (82.60 - 83.40) / 83.40 = -0.80 / 83.40 = -0.00959232  -> -0.9592%
#   rung 3  (82.10 - 83.40) / 83.40 = -1.30 / 83.40 = -0.01558753  -> -1.5588%
#
#   TP1  (85.20 - 82.675) / 82.675 = 2.525 / 82.675 = 0.03054128   -> +3.0541%
#   TP2  (86.60 - 82.675) / 82.675 = 3.925 / 82.675 = 0.04747506   -> +4.7475%
#   TP3  (88.90 - 82.675) / 82.675 = 6.225 / 82.675 = 0.07529483   -> +7.5295%


def test_rung_distances_are_measured_from_the_last_price(
    config: AppConfig, clock: FrozenClock
) -> None:
    plan = plan_for(config, clock, report())
    assert plan.last_price == Decimal("83.40")
    assert [entry.distance_pct for entry in plan.entries] == [
        Decimal("-0.36"),
        Decimal("-0.96"),
        Decimal("-1.56"),
    ]


def test_target_distances_are_measured_from_the_weighted_average_entry(
    config: AppConfig, clock: FrozenClock
) -> None:
    plan = plan_for(config, clock, report())
    assert plan.avg_entry == Decimal("82.675")
    assert plan.target_distances_pct == (
        Decimal("3.05"),
        Decimal("4.75"),
        Decimal("7.53"),
    )


def test_target_distances_are_parallel_to_the_other_target_tuples(
    config: AppConfig, clock: FrozenClock
) -> None:
    """The card zips all four with ``strict=True``; a length drift must fail here."""
    plan = plan_for(config, clock, report())
    assert (
        len(plan.target_distances_pct)
        == len(plan.targets)
        == len(plan.rr_targets)
        == len(plan.rr_targets_net)
    )


def test_the_schema_version_records_the_addition(config: AppConfig, clock: FrozenClock) -> None:
    """2 -> 3. Plans stored by M4/M5.1 stay readable; the new fields are additive."""
    assert plan_for(config, clock, report()).schema_version == 3


def test_every_distance_re_derives_from_the_prices_on_the_plan(
    config: AppConfig, clock: FrozenClock
) -> None:
    """PRD G5: a stored percentage whose reference was thrown away is not auditable."""
    plan = plan_for(config, clock, report())
    for entry in plan.entries:
        assert entry.distance_pct == distance_pct(plan.last_price, entry.price, signed=True)
    for target, shown in zip(plan.targets, plan.target_distances_pct, strict=True):
        assert shown == distance_pct(plan.avg_entry, target, signed=False)


# ── the short mirror ─────────────────────────────────────────────────────── #


def test_a_short_plan_mirrors_the_long_one(config: AppConfig, clock: FrozenClock) -> None:
    """Zone above price, targets below entry — the magnitudes stay positive.

    Rungs sit *above* the last price, so their signed distance is positive; the
    targets are below the entry and still report a positive magnitude, because a
    short's reward is a falling price and a minus sign there would read as a loss.
    """
    short = report(
        direction=Direction.SHORT,
        zone=("81.50", "82.50"),
        stop=("83.40"),
        targets=("79.30", "78.00", "76.00"),
    )
    plan = plan_for(config, clock, short, last_price=("81.20"))

    assert all(entry.distance_pct > 0 for entry in plan.entries)
    assert all(shown > 0 for shown in plan.target_distances_pct)
    for entry in plan.entries:
        assert entry.distance_pct == distance_pct(plan.last_price, entry.price, signed=True)
    for target, shown in zip(plan.targets, plan.target_distances_pct, strict=True):
        assert shown == distance_pct(plan.avg_entry, target, signed=False)


def test_a_rung_above_the_last_price_is_positive(config: AppConfig, clock: FrozenClock) -> None:
    """The sign is not derived from ``direction`` — that is why it is stored.

    §2 rule 2 bounds both zone edges within 3% of the last price; it does not
    force the zone to sit below price for a long. Here the last price is *inside*
    the zone, so a long's ladder straddles it: rung 1 above, rung 3 below. A
    renderer inferring the sign from ``direction`` would print both as negative.
    """
    plan = plan_for(config, clock, report(), last_price="82.60")

    assert plan.entries[0].price == Decimal("83.10")
    assert plan.entries[0].distance_pct > 0
    assert plan.entries[-1].price == Decimal("82.10")
    assert plan.entries[-1].distance_pct < 0


# ── single entry, and the collapse path ──────────────────────────────────── #


def test_a_single_entry_plan_has_one_rung_distance(config: AppConfig, clock: FrozenClock) -> None:
    """Zone narrower than 0.5 x ATR -> one rung at the midpoint (§3).

    Targets are further out than the baseline's on purpose: a single entry at the
    midpoint sits further from the stop than the ladder's weighted average, so the
    baseline's 85.20 would be 1.22R and rejected before it could be measured.
    """
    narrow = report(zone=("82.90", "83.10"), targets=("86.60", "88.00", "90.00"))
    plan = plan_for(config, clock, narrow)

    assert len(plan.entries) == 1
    assert plan.entries[0].price == Decimal("83.00")
    #: (83.00 - 83.40) / 83.40 = -0.40 / 83.40 = -0.00479616 -> -0.4796%
    assert plan.entries[0].distance_pct == Decimal("-0.48")


def test_distances_are_recomputed_after_a_collapse_moves_the_average_entry(
    config: AppConfig, clock: FrozenClock
) -> None:
    """M4 decision 2 applied to the displayed numbers.

    A min-notional collapse drops the far rung and moves ``avg_entry`` towards
    price, which changes every target distance. The plan that ships must show the
    distances of the ladder it ships, not of the one first drafted.
    """
    #: €120 of capital leaves rung 1 below the 20 USDT floor, so the far rung is
    #: dropped and the survivors renormalize to 53.33/46.67 (§3 collapse ruling).
    #: Targets are the wide set again: collapsing moves E *towards* price, which
    #: lowers RR, and the baseline's would fall through §2 rule 5 on the re-check.
    wide = report(targets=("86.60", "88.00", "90.00"))
    full = plan_for(config, clock, wide)
    collapsed = plan_for(config, clock, wide, capital="120")

    assert len(collapsed.entries) < len(full.entries)
    assert collapsed.avg_entry != full.avg_entry
    assert collapsed.target_distances_pct != full.target_distances_pct
    for target, shown in zip(collapsed.targets, collapsed.target_distances_pct, strict=True):
        assert shown == distance_pct(collapsed.avg_entry, target, signed=False)
