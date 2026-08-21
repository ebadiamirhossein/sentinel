"""Costs, execution asymmetry and net RR — FOREX.md §7.3-§7.5, and spec defect #16.

**Defect #16, owner ruling 2026-08-21.** §7.4 computes net RR as ``gross - cost/risk``.
That is not what a round-trip spread does. With bid-referenced levels and asymmetric
execution (§7.5), one spread ``s`` makes the loss ``risk + s`` **and** the gain
``reward - s`` — it lands on both sides, so it is charged ``1 + target`` times when
you invert for the gross needed, not once. ``sentinel/risk/costs.py`` has computed
crypto's net RR this way since M4; forex was the odd one out.

``test_the_corrected_seven_point_four_table`` carries the corrected figures and the
ones §7.4 printed, side by side, so the size of the correction is visible rather than
merely asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.analyst.models import Direction
from sentinel.fx.costs import (
    ExecutionLeg,
    estimate_costs,
    execution_price,
    gross_rr_needed,
    net_rr,
    rollover_nights,
)
from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.sizing import ForexSizing, size_position

NOW = datetime(2026, 8, 21, 7, 0, tzinfo=UTC)
EUR_USD = Decimal("1.169")
EUR_JPY = Decimal("170")
MEASURED = "current measured spread"


def instrument(symbol: str, decimals: int, tick: str) -> ForexInstrument:
    return ForexInstrument(
        symbol=symbol,
        uic={"EURUSD": 21, "GBPUSD": 31, "USDJPY": 42}[symbol],
        decimals=decimals,
        pip=Decimal(1).scaleb(-decimals),
        tick_size=Decimal(tick),
        min_trade_size=Decimal("1000"),
        amount_decimals=2,
        base_currency=symbol[:3],
        quote_currency=symbol[3:],
        resolved_at=NOW,
    )


EURUSD = instrument("EURUSD", 4, "0.00001")
GBPUSD = instrument("GBPUSD", 4, "0.00001")
USDJPY = instrument("USDJPY", 2, "0.001")


def sized(inst: ForexInstrument, entry: str, stop: str, capital: str) -> ForexSizing:
    outcome = size_position(
        instrument=inst,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        capital_eur=Decimal(capital),
        risk_per_trade_pct=Decimal("0.75"),
        eur_quote_rate=EUR_JPY if inst is USDJPY else EUR_USD,
        max_leverage=30,
    )
    assert outcome.sizing is not None
    return outcome.sizing


# ── which side of the spread each leg pays (§7.5) ───────────────────────────


@pytest.mark.parametrize(
    ("direction", "leg", "expected"),
    [
        (Direction.LONG, ExecutionLeg.ENTRY, "1.16977"),  # a long buys at the ask
        (Direction.LONG, ExecutionLeg.EXIT, "1.16965"),  # and sells at the bid
        (Direction.SHORT, ExecutionLeg.ENTRY, "1.16965"),  # a short sells at the bid
        (Direction.SHORT, ExecutionLeg.EXIT, "1.16977"),  # and buys back at the ask
    ],
)
def test_each_execution_leg_transacts_on_the_correct_side(
    direction: Direction, leg: ExecutionLeg, expected: str
) -> None:
    """journal/M10b_SPIKE.md §3's live infoprices control: Bid 1.16965, Ask 1.16977.

    Explicit and tested because getting it wrong shifts every level by the spread —
    one pip on EURUSD, and twelve at rollover on GBPUSD.
    """
    assert execution_price(
        direction=direction, leg=leg, bid=Decimal("1.16965"), ask=Decimal("1.16977")
    ) == Decimal(expected)


def test_a_round_trip_crosses_the_spread_exactly_once_in_either_direction() -> None:
    bid, ask = Decimal("1.16965"), Decimal("1.16977")
    for direction in (Direction.LONG, Direction.SHORT):
        entry = execution_price(direction=direction, leg=ExecutionLeg.ENTRY, bid=bid, ask=ask)
        exit_ = execution_price(direction=direction, leg=ExecutionLeg.EXIT, bid=bid, ask=ask)
        assert abs(entry - exit_) == ask - bid


# ── the cost of one round trip ──────────────────────────────────────────────


def test_the_spread_cost_is_one_full_spread_on_the_whole_position() -> None:
    """EURUSD at EUR 300: 1461.25 units, EUR 0.125 per pip, EUR 2.25 of risk.

    1.1 pips x 0.125 = EUR 0.1375 — which is why costs quantize finer than cents.
    Rounded to 0.14 it would be a tenth of itself out, and net RR would come back
    1.35 where the exact answer is 1.36.
    """
    plan = sized(EURUSD, "1.1692", "1.1674", "300")
    costs = estimate_costs(plan, spread_pips=Decimal("1.1"), spread_basis=MEASURED)

    assert plan.pip_value_eur == Decimal("0.125")
    assert costs.spread_cost_eur == Decimal("0.1375")
    assert costs.total_eur == Decimal("0.1375")
    assert costs.cost_pct_of_risk == Decimal("6.11")
    assert costs.spread_basis == MEASURED


def test_commission_is_a_configured_zero_by_default_and_is_charged_per_leg() -> None:
    """Saxo's standard FX spot pricing is spread-only. A configured zero for an
    account-specific fee is not the same thing as a missing measurement."""
    plan = sized(EURUSD, "1.1692", "1.1674", "300")
    free = estimate_costs(plan, spread_pips=Decimal("1.1"), spread_basis=MEASURED)
    assert free.entry_commission_eur == free.exit_commission_eur == Decimal("0")

    charged = estimate_costs(
        plan,
        spread_pips=Decimal("1.1"),
        spread_basis=MEASURED,
        commission_per_million_quote=Decimal("60"),
    )
    # 1708.49 USD / 1e6 x 60 / 1.169 = EUR 0.0877 per leg, both legs charged.
    assert charged.entry_commission_eur == Decimal("0.0877")
    assert charged.total_eur == Decimal("0.3129")


# ── swap / rollover ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ((17, 10), (18, 10), 1),  # Mon -> Tue: one crossing
        ((18, 10), (20, 10), 4),  # Tue -> Thu: Tuesday 1 + Wednesday 3
        ((14, 10), (17, 10), 1),  # Fri -> Mon: Friday only; the weekend is not charged
        ((17, 10), (17, 20), 0),  # inside one day, no crossing
        ((17, 21), (18, 10), 0),  # a crossing exactly at entry is not charged
        ((17, 10), (24, 10), 7),  # a full week: Mon,Tue,Thu,Fri = 4, plus Wednesday's 3
    ],
    ids=["one-night", "wednesday-triple", "over-a-weekend", "same-day", "at-entry", "full-week"],
)
def test_rollover_nights(start: tuple[int, int], end: tuple[int, int], expected: int) -> None:
    """Charged at 21:00 UTC Monday to Friday and **tripled on Wednesday**, which is
    how the market settles the coming weekend in advance. Saturday and Sunday are
    skipped rather than charged — charging both would double-count the weekend."""
    nights = rollover_nights(
        datetime(2026, 8, start[0], start[1], tzinfo=UTC),
        datetime(2026, 8, end[0], end[1], tzinfo=UTC),
        rollover_hour_utc=21,
    )
    assert nights == expected


def test_a_full_week_is_seven_nights_not_five() -> None:
    """The property behind the parametrized case above, stated so it cannot be read
    as an arbitrary number: Mon+Tue+Thu+Fri = 4 single nights, Wednesday = 3."""
    assert (
        rollover_nights(
            datetime(2026, 8, 17, 10, tzinfo=UTC),
            datetime(2026, 8, 24, 10, tzinfo=UTC),
            rollover_hour_utc=21,
        )
        == 7
    )


def test_a_swap_credit_is_shown_but_never_improves_net_rr() -> None:
    """Same rule and same reasoning as crypto's ``credit_favourable_funding``: an
    estimate of a frequently-flipping input must never be why a plan clears the gate."""
    plan = sized(EURUSD, "1.1692", "1.1674", "300")
    costs = estimate_costs(
        plan,
        spread_pips=Decimal("1.1"),
        spread_basis=MEASURED,
        rollover_pips_per_night=Decimal("-0.4"),
        nights=2,
    )
    assert costs.rollover_eur == Decimal("-0.1")
    assert costs.rollover_charged_eur == Decimal("0")
    assert costs.total_eur == Decimal("0.1375")

    generous = estimate_costs(
        plan,
        spread_pips=Decimal("1.1"),
        spread_basis=MEASURED,
        rollover_pips_per_night=Decimal("-0.4"),
        nights=2,
        credit_favourable_rollover=True,
    )
    assert generous.rollover_charged_eur == Decimal("-0.1")
    assert generous.total_eur == Decimal("0.0375")


def test_a_swap_charge_widens_the_risk() -> None:
    plan = sized(EURUSD, "1.1692", "1.1674", "300")
    costs = estimate_costs(
        plan,
        spread_pips=Decimal("1.1"),
        spread_basis=MEASURED,
        rollover_pips_per_night=Decimal("0.4"),
        nights=2,
    )
    assert costs.rollover_charged_eur == Decimal("0.1")
    assert net_rr(Decimal("1.5"), risk_eur=plan.risk_eur, costs=costs) < net_rr(
        Decimal("1.5"),
        risk_eur=plan.risk_eur,
        costs=estimate_costs(plan, spread_pips=Decimal("1.1"), spread_basis=MEASURED),
    )


# ── net RR: the corrected model (spec defect #16) ───────────────────────────


@pytest.mark.parametrize(
    ("pair", "entry", "stop", "capital", "spread", "expected_net", "expected_gross", "old_net"),
    [
        ("EURUSD", "1.1692", "1.1674", "300", "1.1", "1.36", "1.65", "1.439"),
        ("USDJPY", "148.50", "148.32", "200", "1.5", "1.31", "1.71", "1.417"),
        ("GBPUSD", "1.3450", "1.3432", "300", "1.8", "1.27", "1.75", "1.400"),
        ("GBPUSD", "1.3450", "1.3432", "300", "12.0", "0.50", "3.17", "0.833"),
    ],
    ids=["EURUSD-1.1", "USDJPY-1.5", "GBPUSD-1.8", "GBPUSD-at-21:00"],
)
def test_the_corrected_seven_point_four_table(
    pair: str,
    entry: str,
    stop: str,
    capital: str,
    spread: str,
    expected_net: str,
    expected_gross: str,
    old_net: str,
) -> None:
    """§7.4's table, recomputed under the model §7.5 actually describes.

    All four rows are on an 18-pip stop from a gross 1.5. ``old_net`` is what §7.4
    printed, kept beside the corrected figure so the size of the correction is
    visible: the required uplift is about two and a half times what §7.4 claimed,
    and at GBPUSD's 21:00 rollover a gross 1.5 does not net 0.833 — it nets 0.500.
    """
    inst = {"EURUSD": EURUSD, "GBPUSD": GBPUSD, "USDJPY": USDJPY}[pair]
    plan = sized(inst, entry, stop, capital)
    assert plan.stop_distance_pips == Decimal("18")

    costs = estimate_costs(plan, spread_pips=Decimal(spread), spread_basis=MEASURED)
    got_net = net_rr(Decimal("1.5"), risk_eur=plan.risk_eur, costs=costs)
    got_gross = gross_rr_needed(Decimal("1.5"), risk_eur=plan.risk_eur, costs=costs)

    assert got_net == Decimal(expected_net)
    assert got_gross == Decimal(expected_gross)
    assert got_net < Decimal(old_net), "§7.4's subtraction was optimistic in every row"


def test_net_rr_in_euros_agrees_with_the_same_sum_in_pips() -> None:
    """``(reward - s) / (risk + s)``, which is capital-independent in R terms.

    Two routes to the same number: through euros, pip values and a floored unit
    count, and straight through the pip distances. They must not disagree.
    """
    plan = sized(EURUSD, "1.1692", "1.1674", "300")
    costs = estimate_costs(plan, spread_pips=Decimal("1.1"), spread_basis=MEASURED)
    in_euros = net_rr(Decimal("1.5"), risk_eur=plan.risk_eur, costs=costs)

    stop_pips, spread_pips, gross = Decimal("18"), Decimal("1.1"), Decimal("1.5")
    in_pips = (gross * stop_pips - spread_pips) / (stop_pips + spread_pips)
    assert in_euros == in_pips.quantize(Decimal("0.01"))


def test_a_wider_stop_dilutes_the_spread() -> None:
    """§7.4's one genuinely reassuring line, which survives the correction: the same
    spread costs far less R on a 40-pip stop than on an 18-pip one."""
    tight = sized(EURUSD, "1.1692", "1.1674", "300")
    wide = sized(EURUSD, "1.1692", "1.1652", "1000")
    spread = Decimal("1.1")
    tight_net = net_rr(
        Decimal("1.5"),
        risk_eur=tight.risk_eur,
        costs=estimate_costs(tight, spread_pips=spread, spread_basis=MEASURED),
    )
    wide_net = net_rr(
        Decimal("1.5"),
        risk_eur=wide.risk_eur,
        costs=estimate_costs(wide, spread_pips=spread, spread_basis=MEASURED),
    )
    assert wide.stop_distance_pips == Decimal("40")
    assert wide_net == Decimal("1.43")
    assert wide_net > tight_net


def test_any_positive_cost_sinks_a_gross_one_point_five() -> None:
    """§7.4's qualitative conclusion, which the correction does not disturb."""
    plan = sized(EURUSD, "1.1692", "1.1674", "300")
    costs = estimate_costs(plan, spread_pips=Decimal("0.1"), spread_basis=MEASURED)
    assert net_rr(Decimal("1.5"), risk_eur=plan.risk_eur, costs=costs) < Decimal("1.5")


def test_a_zero_cost_leaves_the_gross_multiple_untouched() -> None:
    """The degenerate case, so the formula cannot be quietly biased."""
    plan = sized(EURUSD, "1.1692", "1.1674", "300")
    costs = estimate_costs(plan, spread_pips=Decimal("0"), spread_basis="none")
    assert net_rr(Decimal("1.5"), risk_eur=plan.risk_eur, costs=costs) == Decimal("1.50")
    assert gross_rr_needed(Decimal("1.5"), risk_eur=plan.risk_eur, costs=costs) == Decimal("1.50")


def test_the_gross_needed_round_trips_through_net_rr() -> None:
    """Whatever ``gross_rr_needed`` says, feeding it back must clear the target."""
    plan = sized(GBPUSD, "1.3450", "1.3432", "300")
    costs = estimate_costs(plan, spread_pips=Decimal("12.0"), spread_basis=MEASURED)
    needed = gross_rr_needed(Decimal("1.5"), risk_eur=plan.risk_eur, costs=costs)
    assert net_rr(needed, risk_eur=plan.risk_eur, costs=costs) >= Decimal("1.5")


def test_there_is_no_funding_field_anywhere_in_the_cost_model() -> None:
    """§7.6 and the owner's ruling: a concept this market does not have gets no field."""
    plan = sized(EURUSD, "1.1692", "1.1674", "300")
    costs = estimate_costs(plan, spread_pips=Decimal("1.1"), spread_basis=MEASURED)
    fields = set(type(costs).model_fields)
    assert not [name for name in fields if "funding" in name or "liq" in name]
    assert "rollover_eur" in fields, "swap is a real forex concept and is named for itself"
