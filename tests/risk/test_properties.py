"""§8.2 property tests — invariants that must hold for *every* valid input.

Scenarios are generated **valid by construction** rather than filtered: an
instrument fixes the tick/step grid, the price range is derived from the tick so
one tick is always a rounding detail rather than a material distance, and the
zone, stop and targets are placed so §2's rules 1-6 pass by arithmetic. What is
left to test is the part that cannot be arranged in advance: sizing, rounding,
leverage and the liquidation buffer.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from hypothesis import assume, given
from hypothesis import strategies as st

from sentinel.analyst.models import (
    AnalystReport,
    CandidateStatus,
    Direction,
    EntryZone,
    SetupType,
    TimeframeLabel,
)
from sentinel.core.clock import FrozenClock
from sentinel.core.config import load_config
from sentinel.risk.engine import RiskEngine
from sentinel.risk.ladder import effective_min_notional
from sentinel.risk.models import (
    AccountState,
    GateStatus,
    MarketContext,
    RejectionReason,
    TradePlan,
)
from sentinel.risk.rounding import money, round_to_tick
from sentinel.risk.sizing import distance_pct

from .conftest import NOW, instrument, portfolio

#: One generated case: the analyst's report plus the market and account it faces.
Scenario = tuple[AnalystReport, MarketContext, AccountState]

CONFIG = load_config()
ENGINE = RiskEngine(CONFIG, clock=FrozenClock(NOW))

INSTRUMENTS = [
    instrument("SOLUSDT", tick_size="0.01", qty_step="0.01", min_notional="5"),
    instrument("BTCUSDT", tick_size="0.1", qty_step="0.001", min_notional="50"),
    instrument("DOGEUSDT", tick_size="0.00001", qty_step="1", min_notional="5"),
    instrument("ETHUSDT", tick_size="0.01", qty_step="0.001", min_notional="20"),
]

#: Rejections the generator can legitimately produce — everything else means the
#: engine and the construction below disagree about what "valid" means.
SIZING_REJECTIONS = {
    RejectionReason.MIN_NOTIONAL,
    RejectionReason.INSUFFICIENT_MARGIN,
    RejectionReason.LIQ_BUFFER,
    # §4.2: targets are generated at 3-4x the risk distance, so gross RR is never
    # the problem — but a tight stop buys enough notional that fees alone can drag
    # a 3R setup under 1.5R net. That is the finding, not a generator defect.
    RejectionReason.NET_RR_TOO_LOW,
}


def dec(low: str, high: str, places: int = 4) -> st.SearchStrategy[Decimal]:
    return st.decimals(
        min_value=Decimal(low), max_value=Decimal(high), places=places, allow_nan=False
    )


@st.composite
def scenario(draw: st.DrawFn) -> Scenario:
    meta = draw(st.sampled_from(INSTRUMENTS))
    tick = meta.tick_size
    direction = draw(st.sampled_from([Direction.LONG, Direction.SHORT]))

    # Price is pinned to the instrument's own grid: >= 20,000 ticks, so a single
    # tick can never dominate an ATR or a stop distance.
    price = round_to_tick(tick * draw(dec("20000", "2000000", places=0)), tick)
    atr = price * draw(dec("0.3", "6.0", places=2)) / Decimal("100")

    # Zone width straddles §3's 0.5 x ATR threshold; capped at 2% of price so
    # both edges stay inside §2 rule 2's 3% budget.
    width = min(atr * draw(dec("0.05", "1.50", places=2)), price * Decimal("0.02"))
    # Stop sits a further 0.65-2.0 ATR beyond the far edge, keeping the distance
    # measured from the weighted entry inside §2 rules 3-4 by construction.
    stop_gap = atr * draw(dec("0.65", "2.00", places=2))
    risk_distance = width + stop_gap

    if direction is Direction.LONG:
        high = round_to_tick(price * (Decimal(1) - draw(dec("0", "0.008"))), tick)
        low = round_to_tick(high - width, tick)
        stop = round_to_tick(low - stop_gap, tick)
        targets = tuple(
            round_to_tick(high + risk_distance * n, tick) for n in (Decimal(3), Decimal(4))
        )
    else:
        low = round_to_tick(price * (Decimal(1) + draw(dec("0", "0.008"))), tick)
        high = round_to_tick(low + width, tick)
        stop = round_to_tick(high + stop_gap, tick)
        targets = tuple(
            round_to_tick(low - risk_distance * n, tick) for n in (Decimal(3), Decimal(4))
        )

    assume(low < high)
    assume(stop > 0 and all(t > 0 for t in targets))
    assume(targets[0] != targets[1])

    report = AnalystReport(
        symbol=meta.symbol,
        candidate_status=CandidateStatus.CANDIDATE,
        setup_type=SetupType.TREND_PULLBACK,
        direction=direction,
        timeframe_label=TimeframeLabel.INTRADAY,
        thesis="generated",
        entry_zone=EntryZone(low=low, high=high),
        stop=stop,
        targets=targets,
        invalidation_price=stop,
        invalidation_text="generated",
        confidence=draw(st.integers(min_value=60, max_value=100)),
    )
    # Funding is drawn across both signs and absent — a long paying, a short being
    # paid, and a snapshot with no derivatives at all all have to hold.
    funding = draw(st.one_of(st.none(), dec("-0.0075", "0.0075", places=7)))
    market = MarketContext(
        symbol=meta.symbol,
        last_price=price,
        atr_1h=atr,
        instrument=meta,
        funding_rate=funding,
        next_funding_time=draw(st.sampled_from([None, NOW + timedelta(hours=3)])),
    )
    account = AccountState(
        capital_eur=draw(dec("500", "1000000", places=2)),
        risk_per_trade_pct=draw(dec("0.25", "1.5", places=2)),
        eurusd_rate=draw(dec("0.8", "1.5")),
    )
    return report, market, account


@given(scenario())
def test_the_gate_is_total_and_only_rejects_for_sizing_reasons(case: Scenario) -> None:
    report, market, account = case
    decision = ENGINE.evaluate(report=report, market=market, account=account, portfolio=portfolio())
    assert decision.status in (GateStatus.APPROVED_FOR_HUMAN, GateStatus.REJECTED)
    if decision.status is GateStatus.REJECTED:
        assert decision.reason in SIZING_REJECTIONS, decision.message


@given(scenario())
def test_actual_risk_never_exceeds_the_configured_risk(case: Scenario) -> None:
    """§8.2 invariant 1 — under budget always, by at most one qty step per rung."""
    _, market, account = case
    plan = approved(case)
    if plan is None:
        return

    assert plan.risk_eur <= plan.planned_risk_eur
    assert market.instrument is not None
    step = market.instrument.qty_step
    tolerance = (
        sum((step * abs(entry.price - plan.stop) for entry in plan.entries), Decimal(0))
        / account.eurusd_rate
    )
    # ... plus one cent, because both figures are reported quantized to cents.
    assert plan.planned_risk_eur - plan.risk_eur <= tolerance + Decimal("0.01")


@given(scenario())
def test_liquidation_is_always_at_least_twice_the_stop_distance(case: Scenario) -> None:
    """§8.2 invariant 2 — a planned stop-out can never reach liquidation."""
    plan = approved(case)
    if plan is None:
        return

    assert 1 <= plan.suggested_leverage <= CONFIG.risk.max_leverage
    assert plan.liq_buffer_ok is True
    assert plan.margin_eur <= plan.capital_eur

    # The invariant, asserted on the EXACT quantities the engine reasoned about
    # rather than on their 2dp display forms: liquidation sits 1/leverage away, and
    # the stop distance is |E - stop| / E. Both are reconstructible from exact
    # stored fields, so this is stronger than comparing the rounded percentages.
    liq_fraction = Decimal(1) / plan.suggested_leverage
    stop_fraction = abs(plan.avg_entry - plan.stop) / plan.avg_entry
    assert liq_fraction >= CONFIG.risk.liq_buffer_multiple * stop_fraction

    # The displayed pair must still agree, to within the rounding that produced it.
    # Percentages are quantized to 2dp (owner ruling 2026-08-18), so each figure can
    # move half a quantum and the doubled one a full quantum — a displayed near-miss
    # at the boundary is a rounding artefact, never a buffer the engine let through.
    quantum = Decimal("0.01")
    assert (
        plan.liq_distance_pct
        >= CONFIG.risk.liq_buffer_multiple * plan.stop_distance_pct - quantum - quantum
    )


@given(scenario())
def test_quantities_reconcile_with_the_notional(case: Scenario) -> None:
    """§8.2 invariant 3 — qty x price sums to the notional, short by <1 step."""
    _, _, account = case
    plan = approved(case)
    if plan is None:
        return

    summed = sum((entry.qty * entry.price for entry in plan.entries), Decimal(0))
    assert summed == plan.notional_usdt
    assert plan.notional_eur == money(plan.notional_usdt / account.eurusd_rate)


@given(scenario())
def test_structural_invariants_of_every_approved_plan(case: Scenario) -> None:
    report, market, _ = case
    plan = approved(case)
    if plan is None:
        return

    assert sum((e.weight_pct for e in plan.entries), Decimal(0)) == Decimal("100")
    assert 1 <= len(plan.entries) <= 3
    assert report.entry_zone is not None
    assert market.instrument is not None
    assert report.entry_zone.low <= plan.avg_entry <= report.entry_zone.high

    floor = effective_min_notional(market.instrument, CONFIG.risk.min_rung_notional_usdt)
    assert all(entry.notional_usdt >= floor for entry in plan.entries)
    assert all(entry.qty > 0 for entry in plan.entries)
    assert plan.rr_targets[0] >= CONFIG.risk.min_rr_tp1
    assert plan.expires_at > plan.created_at


@given(scenario())
def test_distances_are_stored_and_re_derivable(case: Scenario) -> None:
    """§6 addition (2026-08-18, from M6) — the card's percentages are engine output.

    Every figure must both exist and reconcile with the prices stored beside it,
    because the whole point of putting them on the plan is that the bot never
    recomputes them and no one can check them later if their reference is gone.
    """
    _, market, _ = case
    plan = approved(case)
    if plan is None:
        return

    assert plan.last_price == market.last_price
    assert len(plan.target_distances_pct) == len(plan.targets)
    # Targets are beyond the entry in the trade's direction (§2 rule 1), so their
    # distance is a strictly positive magnitude and grows with the target.
    assert all(shown > 0 for shown in plan.target_distances_pct)
    assert list(plan.target_distances_pct) == sorted(plan.target_distances_pct)

    for target, shown in zip(plan.targets, plan.target_distances_pct, strict=True):
        assert shown == distance_pct(plan.avg_entry, target, signed=False)
    for entry in plan.entries:
        assert entry.distance_pct == distance_pct(plan.last_price, entry.price, signed=True)


@given(scenario())
def test_net_rr_never_exceeds_gross_rr(case: Scenario) -> None:
    """§4.2 invariant — costs can only ever make a trade look worse.

    Holds for every target, not just TP1: a smaller numerator over a larger
    denominator. If this ever fails, a cost has gone negative somewhere.
    """
    plan = approved(case)
    if plan is None:
        return

    assert len(plan.rr_targets_net) == len(plan.rr_targets)
    for gross, net in zip(plan.rr_targets, plan.rr_targets_net, strict=True):
        assert net <= gross


@given(scenario())
def test_costs_are_never_negative(case: Scenario) -> None:
    """§4.2 invariant — every component of what the gate charges is >= 0.

    ``funding_eur`` is deliberately exempt: it is the signed estimate, and a short
    collecting funding is a real negative. ``funding_charged_eur`` is the figure
    that reaches net RR, and *that* is floored while ``credit_favourable_funding``
    is false — which is what keeps the invariant above true.
    """
    plan = approved(case)
    if plan is None:
        return

    costs = plan.costs
    assert costs.entry_fee_eur >= 0
    assert costs.stop_exit_fee_eur >= 0
    assert all(fee >= 0 for fee in costs.tp_exit_fees_eur)
    assert costs.round_trip_cost_eur >= 0
    assert costs.cost_pct_of_risk >= 0
    assert costs.funding_settlements >= 0
    if not CONFIG.costs.credit_favourable_funding:
        assert costs.funding_charged_eur >= 0
    assert costs.funding_available is (costs.funding_rate is not None)


@given(scenario())
def test_an_approved_plan_always_clears_the_minimum_on_net_rr(case: Scenario) -> None:
    """§4.2 — the gate is on the net figure, so approval must imply the net bar.

    The gross figure is free to be anything at or above it; only net is promised.
    """
    plan = approved(case)
    if plan is None:
        return

    assert plan.rr_targets_net[0] >= CONFIG.risk.min_rr_tp1
    assert plan.rr_targets[0] >= plan.rr_targets_net[0]


@given(scenario())
def test_a_plan_below_the_minimum_on_net_rr_is_rejected(case: Scenario) -> None:
    """The contrapositive, checked against the engine's own cost arithmetic.

    Re-derives net RR from a *rejected* decision's inputs would just re-run the
    engine; instead this asserts the observable: whenever the gate rejects for
    NET_RR_TOO_LOW it says so with both figures, and no plan escapes.
    """
    report, market, account = case
    decision = ENGINE.evaluate(report=report, market=market, account=account, portfolio=portfolio())
    if decision.reason is not RejectionReason.NET_RR_TOO_LOW:
        return

    assert decision.plan is None
    assert decision.status is GateStatus.REJECTED
    assert "net" in decision.message and "gross" in decision.message


@given(scenario())
def test_no_float_ever_reaches_a_trade_plan(case: Scenario) -> None:
    plan = approved(case)
    if plan is None:
        return
    assert_no_floats(plan.model_dump())


def approved(case: Scenario) -> TradePlan | None:
    report, market, account = case
    decision = ENGINE.evaluate(report=report, market=market, account=account, portfolio=portfolio())
    return decision.plan


def assert_no_floats(value: object, path: str = "plan") -> None:
    if isinstance(value, float):
        raise AssertionError(f"float found at {path}: {value!r}")
    if isinstance(value, dict):
        for key, item in value.items():
            assert_no_floats(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            assert_no_floats(item, f"{path}[{index}]")


# --------------------------------------------------------------------------- #
# Totality under hostile input — the gate must never raise, ever
# --------------------------------------------------------------------------- #


@given(
    low=dec("-1000", "1000"),
    high=dec("-1000", "1000"),
    stop=dec("-1000", "1000"),
    target=dec("-1000", "1000"),
    last_price=dec("-1000", "1000"),
    atr=dec("-100", "100"),
    capital=dec("-1000", "1000000", places=2),
    direction=st.sampled_from([Direction.LONG, Direction.SHORT]),
    confidence=st.integers(min_value=0, max_value=100),
)
def test_absurd_input_is_rejected_not_raised(
    low: Decimal,
    high: Decimal,
    stop: Decimal,
    target: Decimal,
    last_price: Decimal,
    atr: Decimal,
    capital: Decimal,
    direction: Direction,
    confidence: int,
) -> None:
    report = AnalystReport(
        symbol="SOLUSDT",
        candidate_status=CandidateStatus.CANDIDATE,
        setup_type=SetupType.MEAN_REVERSION,
        direction=direction,
        timeframe_label=TimeframeLabel.SWING,
        thesis="hostile",
        entry_zone=EntryZone(low=low, high=high),
        stop=stop,
        targets=(target,),
        invalidation_price=stop,
        invalidation_text="hostile",
        confidence=confidence,
    )
    decision = ENGINE.evaluate(
        report=report,
        market=MarketContext(
            symbol="SOLUSDT",
            last_price=last_price,
            atr_1h=atr,
            instrument=INSTRUMENTS[0],
        ),
        account=AccountState(
            capital_eur=capital,
            risk_per_trade_pct=Decimal("0.75"),
            eurusd_rate=Decimal("1.1593"),
        ),
        portfolio=portfolio(),
    )
    assert isinstance(decision.status, GateStatus)
    if decision.status is not GateStatus.APPROVED_FOR_HUMAN:
        assert isinstance(decision.reason, RejectionReason)
