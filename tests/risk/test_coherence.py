"""§8.3 rejection matrix — one test per coherence rule (specs/RISK_ENGINE.md §2).

Every case starts from the coherent baseline in ``conftest.report()`` and breaks
exactly one thing, so a failure names the rule that broke. Boundaries are pinned
too: a rule that fires *at* its threshold silently rejects valid setups forever.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.analyst.models import AnalystReport, CandidateStatus, Direction
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import (
    AccountState,
    GateDecision,
    GateStatus,
    MarketContext,
    PortfolioState,
    RejectionReason,
)

from .conftest import account, market, portfolio, report

# A deliberately narrow zone: width 0.20 < 0.5 x ATR(0.90), so the ladder is a
# single entry at the midpoint 82.10 and every distance below is exact by hand.
NARROW = ("82.00", "82.20")
MIDPOINT = Decimal("82.10")


def decide(
    config: AppConfig,
    clock: FrozenClock,
    *,
    rep: AnalystReport | None = None,
    mkt: MarketContext | None = None,
    acc: AccountState | None = None,
    pf: PortfolioState | None = None,
) -> GateDecision:
    return RiskEngine(config, clock=clock).evaluate(
        report=rep if rep is not None else report(),
        market=mkt if mkt is not None else market(),
        account=acc if acc is not None else account(),
        portfolio=pf if pf is not None else portfolio(),
    )


# --------------------------------------------------------------------------- #
# Preconditions (before any rule can be evaluated)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status",
    [CandidateStatus.WATCHLIST, CandidateStatus.NO_SETUP],
)
def test_only_candidates_are_gated(
    config: AppConfig, clock: FrozenClock, status: CandidateStatus
) -> None:
    decision = decide(config, clock, rep=report(status=status))
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.NOT_A_CANDIDATE


def test_missing_plan_fields_are_rejected(config: AppConfig, clock: FrozenClock) -> None:
    broken = report().model_copy(update={"stop": None})
    decision = decide(config, clock, rep=broken)
    assert decision.reason is RejectionReason.MISSING_PLAN_FIELDS


def test_no_targets_is_rejected(config: AppConfig, clock: FrozenClock) -> None:
    broken = report().model_copy(update={"targets": ()})
    decision = decide(config, clock, rep=broken)
    assert decision.reason is RejectionReason.MISSING_PLAN_FIELDS


def test_capital_must_be_set_before_the_first_signal(config: AppConfig, clock: FrozenClock) -> None:
    decision = decide(config, clock, acc=account(capital_eur=None))
    assert decision.reason is RejectionReason.NO_CAPITAL


def test_atr_is_required_by_rules_3_and_4(config: AppConfig, clock: FrozenClock) -> None:
    decision = decide(config, clock, mkt=market().model_copy(update={"atr_1h": None}))
    assert decision.reason is RejectionReason.ATR_UNAVAILABLE


def test_instrument_rules_are_required_for_rounding(config: AppConfig, clock: FrozenClock) -> None:
    decision = decide(config, clock, mkt=market().model_copy(update={"instrument": None}))
    assert decision.reason is RejectionReason.INSTRUMENT_META_MISSING


# --------------------------------------------------------------------------- #
# Rule 1 — geometry
# --------------------------------------------------------------------------- #


def test_rule1_long_stop_must_sit_below_the_zone(config: AppConfig, clock: FrozenClock) -> None:
    decision = decide(config, clock, rep=report(stop="82.50"))
    assert decision.reason is RejectionReason.STOP_SIDE


def test_rule1_short_stop_must_sit_above_the_zone(config: AppConfig, clock: FrozenClock) -> None:
    short = report(
        direction=Direction.SHORT,
        zone=("82.10", "83.10"),
        stop="82.50",
        targets=("80.50", "79.20"),
    )
    decision = decide(config, clock, rep=short)
    assert decision.reason is RejectionReason.STOP_SIDE


def test_rule1_zone_must_be_ordered(config: AppConfig, clock: FrozenClock) -> None:
    decision = decide(config, clock, rep=report(zone=("83.10", "82.10")))
    assert decision.reason is RejectionReason.ENTRY_ZONE_INVALID


def test_rule1_long_targets_must_clear_the_zone(config: AppConfig, clock: FrozenClock) -> None:
    decision = decide(config, clock, rep=report(targets=("83.00", "86.60")))
    assert decision.reason is RejectionReason.TARGET_ORDER


def test_rule1_targets_must_be_monotonic(config: AppConfig, clock: FrozenClock) -> None:
    decision = decide(config, clock, rep=report(targets=("86.60", "84.90")))
    assert decision.reason is RejectionReason.TARGET_ORDER


def test_rule1_short_targets_must_sit_below_the_zone(config: AppConfig, clock: FrozenClock) -> None:
    short = report(
        direction=Direction.SHORT,
        zone=("83.60", "84.60"),
        stop="86.00",
        targets=("84.00",),
    )
    decision = decide(config, clock, rep=short)
    assert decision.reason is RejectionReason.TARGET_ORDER


def test_rule1_short_targets_must_be_monotonic(config: AppConfig, clock: FrozenClock) -> None:
    short = report(
        direction=Direction.SHORT,
        zone=("83.60", "84.60"),
        stop="86.10",
        targets=("79.20", "80.90"),  # second target is nearer, not further
    )
    decision = decide(config, clock, rep=short)
    assert decision.reason is RejectionReason.TARGET_ORDER


def test_a_non_positive_last_price_cannot_be_measured_against(
    config: AppConfig, clock: FrozenClock
) -> None:
    """Missing market data must reject, never divide by zero."""
    decision = decide(config, clock, mkt=market(last_price="0"))
    assert decision.reason is RejectionReason.ENTRY_TOO_FAR


def test_short_baseline_is_coherent(config: AppConfig, clock: FrozenClock) -> None:
    """Mirror of the long baseline — proves the short path is not simply broken."""
    short = report(
        direction=Direction.SHORT,
        zone=("83.60", "84.60"),
        stop="86.10",
        targets=("80.90", "79.20", "77.50"),
    )
    decision = decide(config, clock, rep=short, mkt=market(last_price="83.40"))
    assert decision.status is GateStatus.APPROVED_FOR_HUMAN


# --------------------------------------------------------------------------- #
# Rule 2 — entry distance (owner decision: BOTH edges within the budget)
# --------------------------------------------------------------------------- #


def test_rule2_rejects_a_zone_beyond_the_distance_budget(
    config: AppConfig, clock: FrozenClock
) -> None:
    # 80.89 is 3.01% below 83.40 — one tick past the 3% budget.
    far = report(zone=("80.89", "81.00"), stop="80.35", targets=("81.70", "82.40"))
    decision = decide(config, clock, rep=far)
    assert decision.reason is RejectionReason.ENTRY_TOO_FAR


def test_rule2_accepts_a_zone_exactly_at_the_budget(config: AppConfig, clock: FrozenClock) -> None:
    # 80.90 is 2.998% below 83.40 — inside the budget, must not be rejected.
    edge = report(zone=("80.90", "81.00"), stop="80.36", targets=("81.76", "82.40"))
    decision = decide(config, clock, rep=edge)
    assert decision.reason is not RejectionReason.ENTRY_TOO_FAR


def test_rule2_measures_the_far_edge_too(config: AppConfig, clock: FrozenClock) -> None:
    """A zone whose near edge is close but whose far edge is fantasy is rejected."""
    stretched = report(zone=("80.50", "83.10"), stop="79.80", targets=("85.00", "87.00"))
    decision = decide(config, clock, rep=stretched)
    assert decision.reason is RejectionReason.ENTRY_TOO_FAR


# --------------------------------------------------------------------------- #
# Rules 3 & 4 — stop distance in ATR multiples, measured from the weighted entry
# --------------------------------------------------------------------------- #


def test_rule3_rejects_a_noise_level_stop(config: AppConfig, clock: FrozenClock) -> None:
    # E = 82.10, stop 81.57 -> 0.53 = 0.589 x ATR(0.90), below the 0.6 floor.
    tight = report(zone=NARROW, stop="81.57", targets=("83.00", "84.00"))
    decision = decide(config, clock, rep=tight)
    assert decision.reason is RejectionReason.STOP_TOO_TIGHT


def test_rule3_accepts_exactly_the_minimum_multiple(config: AppConfig, clock: FrozenClock) -> None:
    # 82.10 - 0.54 = 81.56 -> exactly 0.6 x ATR.
    at_floor = report(zone=NARROW, stop="81.56", targets=("82.91", "83.50"))
    decision = decide(config, clock, rep=at_floor)
    assert decision.reason is not RejectionReason.STOP_TOO_TIGHT


def test_rule4_rejects_a_lazy_wide_stop(config: AppConfig, clock: FrozenClock) -> None:
    # 82.10 - 2.71 = 79.39 -> 3.011 x ATR, past the 3.0 ceiling.
    wide = report(zone=NARROW, stop="79.39", targets=("86.30", "90.00"))
    decision = decide(config, clock, rep=wide)
    assert decision.reason is RejectionReason.STOP_TOO_WIDE


def test_rule4_accepts_exactly_the_maximum_multiple(config: AppConfig, clock: FrozenClock) -> None:
    # 82.10 - 2.70 = 79.40 -> exactly 3.0 x ATR.
    at_ceiling = report(zone=NARROW, stop="79.40", targets=("86.20", "90.00"))
    decision = decide(config, clock, rep=at_ceiling)
    assert decision.reason is not RejectionReason.STOP_TOO_WIDE


# --------------------------------------------------------------------------- #
# Rule 5 — RR to TP1 from the weighted average entry
# --------------------------------------------------------------------------- #


def test_rule5_rejects_thin_reward(config: AppConfig, clock: FrozenClock) -> None:
    # E = 82.10, stop 81.56 (risk 0.54); TP1 82.90 -> RR 1.481.
    thin = report(zone=NARROW, stop="81.56", targets=("82.90", "84.00"))
    decision = decide(config, clock, rep=thin)
    assert decision.reason is RejectionReason.RR_TOO_LOW


def test_rule5_accepts_exactly_the_minimum_rr(config: AppConfig, clock: FrozenClock) -> None:
    # TP1 82.91 -> RR exactly 1.5.
    at_min = report(zone=NARROW, stop="81.56", targets=("82.91", "84.00"))
    decision = decide(config, clock, rep=at_min)
    assert decision.status is GateStatus.APPROVED_FOR_HUMAN
    assert decision.plan is not None
    assert decision.plan.rr_targets[0] == Decimal("1.5")


# --------------------------------------------------------------------------- #
# Rule 6 — confidence gate (downgrade, not rejection)
# --------------------------------------------------------------------------- #


def test_rule6_downgrades_low_confidence_to_watchlist(
    config: AppConfig, clock: FrozenClock
) -> None:
    decision = decide(config, clock, rep=report(confidence=59))
    assert decision.status is GateStatus.DOWNGRADED_WATCHLIST
    assert decision.reason is RejectionReason.LOW_CONFIDENCE
    assert decision.plan is None


def test_rule6_accepts_exactly_the_minimum_confidence(
    config: AppConfig, clock: FrozenClock
) -> None:
    decision = decide(config, clock, rep=report(confidence=60))
    assert decision.status is GateStatus.APPROVED_FOR_HUMAN


# --------------------------------------------------------------------------- #
# Every decision carries a machine-readable code for M9's stats
# --------------------------------------------------------------------------- #


def test_non_approved_decisions_always_carry_a_reason_code(
    config: AppConfig, clock: FrozenClock
) -> None:
    for rep in (
        report(status=CandidateStatus.NO_SETUP),
        report(stop="82.50"),
        report(confidence=10),
        report(zone=NARROW, stop="81.57", targets=("83.00", "84.00")),
    ):
        decision = decide(config, clock, rep=rep)
        assert decision.status is not GateStatus.APPROVED_FOR_HUMAN
        assert isinstance(decision.reason, RejectionReason)
        assert decision.message  # human-readable, but never the only record
