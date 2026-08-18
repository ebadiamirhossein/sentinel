"""§4.2 transaction costs — fees, funding, and the net-RR gate.

Fee arithmetic is hand-calculated in each docstring, the same discipline §8.1
uses: a golden generated from the code it tests only proves the code agrees with
itself. Funding is the one estimate in the module, so its tests are about
*counting settlements correctly* and *degrading honestly*, not about predicting a
rate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.analyst.models import Direction
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, CostsConfig
from sentinel.risk.accounting import Exit, Fill
from sentinel.risk.costs import (
    estimate_funding_eur,
    fee_eur,
    funding_settlements,
    net_rr_multiples,
    realized_costs_eur,
)
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import EntryRung, GateStatus, PlanCosts, RejectionReason
from sentinel.risk.rounding import percent

from .conftest import NOW, account, market, portfolio, report

# --------------------------------------------------------------------------- #
# Fees
# --------------------------------------------------------------------------- #


def test_fee_is_a_percentage_of_notional() -> None:
    """0.02% of €4,570.30 = €0.914060 — exact, not rounded at this layer."""
    assert fee_eur(Decimal("4570.30"), Decimal("0.02")) == Decimal("0.914060")
    assert fee_eur(Decimal("4570.30"), Decimal("0.05")) == Decimal("2.285150")


def test_a_zero_fee_costs_nothing() -> None:
    assert fee_eur(Decimal("10000"), Decimal("0")) == Decimal("0")


def test_the_configured_defaults_are_the_verified_binance_rates(config: AppConfig) -> None:
    """Binance USDⓈ-M VIP 0, verified 2026-08-18 against the published schedule.

    Pinned as a test because the whole cost model is calibrated on them: if
    someone edits config.yaml to a VIP tier or a BNB-discounted rate, every stored
    net RR silently changes meaning.
    """
    assert config.costs.maker_fee_pct == Decimal("0.02")
    assert config.costs.taker_fee_pct == Decimal("0.05")
    assert config.costs.funding_interval_hours == 8
    assert config.costs.credit_favourable_funding is False


# --------------------------------------------------------------------------- #
# Funding — settlement counting
# --------------------------------------------------------------------------- #


def test_settlements_are_counted_from_the_exchanges_next_one() -> None:
    """NOW 12:00, next settlement 16:00, 8h interval, 12h TTL → 16:00 and 00:00 = 2."""
    assert (
        funding_settlements(
            created_at=NOW,
            expires_at=NOW + timedelta(hours=12),
            next_funding_time=datetime(2026, 8, 18, 16, 0, tzinfo=UTC),
            interval_hours=8,
        )
        == 2
    )


def test_a_settlement_just_inside_the_window_is_charged() -> None:
    """A plan created five minutes before a settlement pays it. Inclusive at the end."""
    assert (
        funding_settlements(
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            next_funding_time=NOW + timedelta(minutes=5),
            interval_hours=8,
        )
        == 1
    )


def test_a_window_that_spans_no_settlement_costs_no_funding() -> None:
    assert (
        funding_settlements(
            created_at=NOW,
            expires_at=NOW + timedelta(hours=3),
            next_funding_time=NOW + timedelta(hours=4),
            interval_hours=8,
        )
        == 0
    )


def test_a_settlement_already_past_is_not_owed() -> None:
    """A stale ``next_funding_time`` is walked forward, never charged retroactively."""
    assert (
        funding_settlements(
            created_at=NOW,
            expires_at=NOW + timedelta(hours=12),
            next_funding_time=NOW - timedelta(hours=20),
            interval_hours=8,
        )
        == 2
    )


def test_without_a_published_settlement_the_window_is_divided() -> None:
    """No anchor → 36h / 8h = 4. The average count, never a fabricated schedule."""
    assert (
        funding_settlements(
            created_at=NOW,
            expires_at=NOW + timedelta(hours=36),
            next_funding_time=None,
            interval_hours=8,
        )
        == 4
    )


@pytest.mark.parametrize("interval", [0, -8])
def test_a_nonsensical_interval_yields_no_settlements(interval: int) -> None:
    assert (
        funding_settlements(
            created_at=NOW,
            expires_at=NOW + timedelta(hours=12),
            next_funding_time=None,
            interval_hours=interval,
        )
        == 0
    )


def test_an_expiry_at_or_before_creation_yields_no_settlements() -> None:
    assert (
        funding_settlements(
            created_at=NOW,
            expires_at=NOW,
            next_funding_time=NOW,
            interval_hours=8,
        )
        == 0
    )


# --------------------------------------------------------------------------- #
# Funding — sign and availability
# --------------------------------------------------------------------------- #


def test_a_long_pays_funding_when_the_rate_is_positive() -> None:
    """€10,000 x 0.01% x 2 settlements = €2.00 out."""
    assert estimate_funding_eur(
        notional_eur=Decimal("10000"),
        funding_rate=Decimal("0.0001"),
        settlements=2,
        direction=Direction.LONG,
    ) == Decimal("2.00")


def test_a_short_receives_funding_when_the_rate_is_positive() -> None:
    """The mirror image, and the reason the figure is signed rather than absolute."""
    assert estimate_funding_eur(
        notional_eur=Decimal("10000"),
        funding_rate=Decimal("0.0001"),
        settlements=2,
        direction=Direction.SHORT,
    ) == Decimal("-2.00")


def test_a_long_receives_funding_when_the_rate_is_negative() -> None:
    assert estimate_funding_eur(
        notional_eur=Decimal("10000"),
        funding_rate=Decimal("-0.0001"),
        settlements=2,
        direction=Direction.LONG,
    ) == Decimal("-2.00")


def test_an_unavailable_rate_is_zero_never_a_guess() -> None:
    """CLAUDE.md: degrade explicitly. The caller flags it; nothing is invented."""
    assert (
        estimate_funding_eur(
            notional_eur=Decimal("10000"),
            funding_rate=None,
            settlements=2,
            direction=Direction.LONG,
        )
        == 0
    )


def test_no_settlements_means_no_funding() -> None:
    assert (
        estimate_funding_eur(
            notional_eur=Decimal("10000"),
            funding_rate=Decimal("0.0001"),
            settlements=0,
            direction=Direction.LONG,
        )
        == 0
    )


# --------------------------------------------------------------------------- #
# Net RR
# --------------------------------------------------------------------------- #


def costs(
    *,
    entry: str = "0.91",
    stop_exit: str = "2.25",
    tp_exits: tuple[str, ...] = ("2.36",),
    funding_charged: str = "0",
) -> PlanCosts:
    return PlanCosts(
        maker_fee_pct=Decimal("0.02"),
        taker_fee_pct=Decimal("0.05"),
        entry_fee_eur=Decimal(entry),
        stop_exit_fee_eur=Decimal(stop_exit),
        tp_exit_fees_eur=tuple(Decimal(fee) for fee in tp_exits),
        funding_charged_eur=Decimal(funding_charged),
    )


def test_net_rr_matches_the_hand_calculation() -> None:
    """(1.71 x 74.98 - 0.91 - 2.36) / (74.98 + 0.91 + 2.25) = 124.95/78.14 = 1.60."""
    assert net_rr_multiples(
        rr_gross=(Decimal("1.71"),), risk_eur=Decimal("74.98"), costs=costs()
    ) == (Decimal("1.60"),)


def test_funding_enters_both_sides_of_net_rr() -> None:
    """Funding is paid whether the trade wins or loses: out of reward, into risk.

    (1.71x74.98 - 0.91 - 2.36 - 5.00) / (74.98 + 0.91 + 2.25 + 5.00)
    = 119.95/83.14 = 1.44 — under the bar that the same setup cleared at 1.60.
    """
    assert net_rr_multiples(
        rr_gross=(Decimal("1.71"),),
        risk_eur=Decimal("74.98"),
        costs=costs(funding_charged="5.00"),
    ) == (Decimal("1.44"),)


def test_zero_costs_leave_rr_untouched() -> None:
    """The degenerate case: with no fees, net RR is gross RR exactly."""
    assert net_rr_multiples(
        rr_gross=(Decimal("1.71"), Decimal("2.66")),
        risk_eur=Decimal("75"),
        costs=costs(entry="0", stop_exit="0", tp_exits=("0", "0")),
    ) == (Decimal("1.71"), Decimal("2.66"))


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def test_funding_pushes_a_marginal_plan_under_the_bar(
    config: AppConfig, clock: FrozenClock
) -> None:
    """The baseline approves at 1.60 net on fees alone; an extreme long-pays rate
    over a 12h window adds enough cost to reject it.

    0.75% per settlement is near Binance's cap — deliberately extreme, because the
    point is that funding *reaches* the gate at all, not that this rate is typical.
    """
    engine = RiskEngine(config, clock=clock)
    funded = market(funding_rate="0.0075", next_funding_time=NOW + timedelta(hours=1))

    decision = engine.evaluate(
        report=report(), market=funded, account=account(), portfolio=portfolio()
    )

    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.NET_RR_TOO_LOW


def test_a_short_collecting_funding_is_shown_the_credit_but_not_gated_on_it(
    config: AppConfig, clock: FrozenClock
) -> None:
    """The signed estimate reaches the card; the gate charges max(0, funding).

    Confirms the two fields say different things on purpose — the owner sees the
    credit, ``min_rr_tp1`` does not spend it.
    """
    engine = RiskEngine(config, clock=clock)
    short = report(
        direction=Direction.SHORT,
        zone=("83.60", "84.60"),
        stop="86.10",
        targets=("80.60", "79.20", "77.50"),
    )
    funded = market(funding_rate="0.0005", next_funding_time=NOW + timedelta(hours=1))

    decision = engine.evaluate(
        report=short, market=funded, account=account(), portfolio=portfolio()
    )

    assert decision.plan is not None
    plan_costs = decision.plan.costs
    assert plan_costs.funding_available is True
    assert plan_costs.funding_eur < 0  # a credit, honestly reported
    assert plan_costs.funding_charged_eur == 0  # and not spent by the gate
    assert plan_costs.round_trip_cost_eur == (
        plan_costs.entry_fee_eur + plan_costs.stop_exit_fee_eur
    )


def test_crediting_favourable_funding_is_switchable(config: AppConfig, clock: FrozenClock) -> None:
    """With the switch on, a short's funding credit does improve net RR.

    The config knob exists so the decision is visible and reversible rather than
    buried in the arithmetic.
    """
    crediting = config.model_copy(update={"costs": CostsConfig(credit_favourable_funding=True)})
    short = report(
        direction=Direction.SHORT,
        zone=("83.60", "84.60"),
        stop="86.10",
        targets=("80.60", "79.20", "77.50"),
    )
    funded = market(funding_rate="0.0005", next_funding_time=NOW + timedelta(hours=1))

    default = RiskEngine(config, clock=clock).evaluate(
        report=short, market=funded, account=account(), portfolio=portfolio()
    )
    credited = RiskEngine(crediting, clock=clock).evaluate(
        report=short, market=funded, account=account(), portfolio=portfolio()
    )

    assert default.plan is not None and credited.plan is not None
    assert credited.plan.costs.funding_charged_eur < 0
    assert credited.plan.rr_targets_net[0] > default.plan.rr_targets_net[0]


def test_a_plan_without_funding_data_still_gets_its_fees_costed(
    config: AppConfig, clock: FrozenClock
) -> None:
    """A missing rate degrades the funding line only — fees are never optional."""
    decision = RiskEngine(config, clock=clock).evaluate(
        report=report(), market=market(), account=account(), portfolio=portfolio()
    )

    assert decision.plan is not None
    plan_costs = decision.plan.costs
    assert plan_costs.funding_available is False
    assert plan_costs.funding_rate is None
    assert plan_costs.funding_settlements == 0
    assert plan_costs.entry_fee_eur > 0
    assert plan_costs.stop_exit_fee_eur > 0


def test_the_cost_line_is_a_share_of_the_planned_budget(
    config: AppConfig, clock: FrozenClock
) -> None:
    """§4.2's headline number: what fraction of the risk budget the trade costs."""
    decision = RiskEngine(config, clock=clock).evaluate(
        report=report(), market=market(), account=account(), portfolio=portfolio()
    )

    assert decision.plan is not None
    plan = decision.plan
    # Recomputed independently, then quantized the way every human-facing
    # percentage is (2dp, owner ruling 2026-08-18 — see risk/rounding.percent).
    expected = percent(plan.costs.round_trip_cost_eur / plan.planned_risk_eur * Decimal("100"))
    assert plan.costs.cost_pct_of_risk == expected


def test_entry_fees_cover_every_rung(config: AppConfig, clock: FrozenClock) -> None:
    """The maker fee is charged per rung, on that rung's own notional."""
    decision = RiskEngine(config, clock=clock).evaluate(
        report=report(), market=market(), account=account(), portfolio=portfolio()
    )

    assert decision.plan is not None
    plan = decision.plan
    assert len(plan.entries) == 3
    by_rung = sum(
        (fee_eur(entry.notional_eur, plan.costs.maker_fee_pct) for entry in plan.entries),
        Decimal(0),
    )
    assert plan.costs.entry_fee_eur == by_rung.quantize(Decimal("0.01"))


def test_entry_rung_fees_are_decimal_all_the_way_down() -> None:
    """No float creeps in through the fee path (CLAUDE.md)."""
    rung = EntryRung(
        price=Decimal("83.10"),
        weight_pct=Decimal("40"),
        qty=Decimal("18.30"),
        notional_usdt=Decimal("1520.730"),
        notional_eur=Decimal("1311.77"),
    )
    assert isinstance(fee_eur(rung.notional_eur, Decimal("0.02")), Decimal)


# --------------------------------------------------------------------------- #
# Realized costs (M5.1 §10 — "M7's tracker should charge real costs")
# --------------------------------------------------------------------------- #
#
# Every number below is hand-calculated in its docstring before the assertion, on
# the M4 golden ladder: 18.30 @ 83.10, 21.73 @ 82.60, 24.15 @ 82.10, stop 81.20,
# EURUSD 1.1593, maker 0.02%, taker 0.05%.


def _fills(*legs: tuple[str, str]) -> tuple[Fill, ...]:
    return tuple(Fill(price=Decimal(p), qty=Decimal(q)) for p, q in legs)


RUNG1 = ("83.10", "18.30")
RUNG2 = ("82.60", "21.73")
RUNG3 = ("82.10", "24.15")


def test_a_full_ladder_stopped_out_costs_what_the_plan_estimated(config: AppConfig) -> None:
    """The estimate and the realization agree when the plan happens exactly.

    filled notional = 18.30(83.10) + 21.73(82.60) + 24.15(82.10)
                    = 1520.730 + 1794.898 + 1982.715 = 5298.343 USDT
                    = 5298.343 / 1.1593 = EUR 4570.29932
    entry fee       = 4570.29932 x 0.02%              = EUR 0.91406
    exit  notional  = 64.18 x 81.20 = 5211.416 USDT   = EUR 4495.31355
    exit  fee       = 4495.31355 x 0.05%              = EUR 2.24766
    total                                              = EUR 3.16
    """
    costs = realized_costs_eur(
        direction=Direction.LONG,
        fills=_fills(RUNG1, RUNG2, RUNG3),
        exits=(Exit(price=Decimal("81.20"), qty=Decimal("64.18")),),
        eurusd_rate=Decimal("1.1593"),
        funding_rate=None,
        settlements=0,
        config=config.costs,
    )
    assert costs.entry_fee_eur == Decimal("0.91")
    assert costs.exit_fee_eur == Decimal("2.25")
    assert costs.total_eur == Decimal("3.16")
    assert costs.funding_available is False


def test_a_rung_one_stop_out_is_charged_on_what_actually_filled(config: AppConfig) -> None:
    """The whole point of realizing costs: two thirds of the ladder never filled,
    so two thirds of the estimated fee was never paid.

    filled notional = 18.30 x 83.10 = 1520.730 USDT = EUR 1311.76573
    entry fee       = 1311.76573 x 0.02%            = EUR 0.26235
    exit  notional  = 18.30 x 81.20 = 1485.960 USDT = EUR 1281.77347
    exit  fee       = 1281.77347 x 0.05%            = EUR 0.64089
    total                                            = EUR 0.90
    """
    costs = realized_costs_eur(
        direction=Direction.LONG,
        fills=_fills(RUNG1),
        exits=(Exit(price=Decimal("81.20"), qty=Decimal("18.30")),),
        eurusd_rate=Decimal("1.1593"),
        funding_rate=None,
        settlements=0,
        config=config.costs,
    )
    assert costs.entry_fee_eur == Decimal("0.26")
    assert costs.exit_fee_eur == Decimal("0.64")
    assert costs.total_eur == Decimal("0.90")


def test_a_signal_that_never_filled_costs_nothing(config: AppConfig) -> None:
    """An expired-unfilled signal is not a trade. Charging it a fee would put a
    loss in the statistics for an order that never existed."""
    costs = realized_costs_eur(
        direction=Direction.LONG,
        fills=(),
        exits=(),
        eurusd_rate=Decimal("1.1593"),
        funding_rate=Decimal("0.0000193"),
        settlements=3,
        config=config.costs,
    )
    assert costs.entry_fee_eur == Decimal("0")
    assert costs.exit_fee_eur == Decimal("0")
    assert costs.funding_eur == Decimal("0")
    assert costs.total_eur == Decimal("0")


def test_an_open_position_is_charged_its_entry_fee_but_no_exit_fee(config: AppConfig) -> None:
    """Realized costs follow realized legs. A filled position still running has
    paid to get in and has not yet paid to get out."""
    costs = realized_costs_eur(
        direction=Direction.LONG,
        fills=_fills(RUNG1),
        exits=(),
        eurusd_rate=Decimal("1.1593"),
        funding_rate=None,
        settlements=0,
        config=config.costs,
    )
    assert costs.entry_fee_eur == Decimal("0.26")
    assert costs.exit_fee_eur == Decimal("0")
    assert costs.total_eur == Decimal("0.26")


def test_a_long_pays_funding_over_the_settlements_it_actually_held_through(
    config: AppConfig,
) -> None:
    """funding = 1311.76573 x 0.0000193 x 2 = EUR 0.05063 -> paid by the long."""
    costs = realized_costs_eur(
        direction=Direction.LONG,
        fills=_fills(RUNG1),
        exits=(),
        eurusd_rate=Decimal("1.1593"),
        funding_rate=Decimal("0.0000193"),
        settlements=2,
        config=config.costs,
    )
    assert costs.funding_available is True
    assert costs.funding_settlements == 2
    assert costs.funding_eur == Decimal("0.05")
    assert costs.funding_charged_eur == Decimal("0.05")
    assert costs.total_eur == Decimal("0.31")  # 0.26235 + 0 + 0.05063


def test_a_short_receives_that_funding_and_is_not_credited_for_it(config: AppConfig) -> None:
    """The signed figure is the honest one and the card shows it; ``total_eur``
    still spends ``max(0, funding)`` because ``credit_favourable_funding`` is
    false. §4.2's rule applies to a realized credit exactly as to an estimated
    one — otherwise a short's measured cost would depend on a rate that flips."""
    costs = realized_costs_eur(
        direction=Direction.SHORT,
        fills=_fills(RUNG1),
        exits=(),
        eurusd_rate=Decimal("1.1593"),
        funding_rate=Decimal("0.0000193"),
        settlements=2,
        config=config.costs,
    )
    assert costs.funding_eur == Decimal("-0.05")
    assert costs.funding_charged_eur == Decimal("0")
    assert costs.total_eur == Decimal("0.26")


def test_a_credit_may_reduce_realized_cost_when_the_switch_says_so(config: AppConfig) -> None:
    generous = config.costs.model_copy(update={"credit_favourable_funding": True})
    costs = realized_costs_eur(
        direction=Direction.SHORT,
        fills=_fills(RUNG1),
        exits=(),
        eurusd_rate=Decimal("1.1593"),
        funding_rate=Decimal("0.0000193"),
        settlements=2,
        config=generous,
    )
    assert costs.funding_charged_eur == Decimal("-0.05")
    assert costs.total_eur == Decimal("0.21")  # 0.26235 - 0.05063


def test_a_missing_funding_rate_is_unavailable_rather_than_zero(config: AppConfig) -> None:
    """DATA_SOURCES §4: degrade explicitly. A stored 0.00 would read as "funding
    was measured and it was free"."""
    costs = realized_costs_eur(
        direction=Direction.LONG,
        fills=_fills(RUNG1),
        exits=(),
        eurusd_rate=Decimal("1.1593"),
        funding_rate=None,
        settlements=6,
        config=config.costs,
    )
    assert costs.funding_available is False
    assert costs.funding_settlements == 0
    assert costs.funding_eur == Decimal("0")
