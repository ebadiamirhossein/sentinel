"""The forex gate, row by row (FOREX.md §16.5).

**Every rejection is tested with its non-rejection sibling.** A gate test that only ever
shows a rail firing proves the rail exists and not that it discriminates — and a rail
that rejects everything looks identical to a working one until the day a real setup
arrives. So the pattern throughout is: the baseline approves, and one input moved across
one edge rejects.

The baseline lives in ``tests/fx/forex_double.py`` and it goes through the **real** gate.
Nothing in this suite fabricates a plan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.analyst.models import (
    AnalystReport,
    CandidateStatus,
    Direction,
    TimeframeLabel,
)
from sentinel.core.config import AppConfig, load_config
from sentinel.fx.gate import MESSAGES, ForexGate
from sentinel.fx.models import ForexGateStatus, ForexRejection
from sentinel.fx.rails import ForexPortfolioState
from sentinel.fx.spread import SpreadProfile
from sentinel.risk.models import GateStatus
from tests.fx.forex_double import (
    FX_NOW,
    USDJPY,
    account,
    analyst_report,
    calendar,
    decide,
    high_impact,
    market_context,
    spread_profile,
)


@pytest.fixture
def config() -> AppConfig:
    return load_config()


def reason(**kwargs: object) -> ForexRejection | None:
    return decide(**kwargs).reason  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The baseline approves — everything below is one step off it
# --------------------------------------------------------------------------- #


def test_the_baseline_setup_is_approved() -> None:
    """Without this, every rejection test below could be passing for the wrong reason."""
    decision = decide()

    assert decision.status is ForexGateStatus.APPROVED_FOR_HUMAN
    assert decision.reason is None
    assert decision.plan is not None
    assert decision.plan.symbol == "EURUSD"


# --------------------------------------------------------------------------- #
# Row 1 — preconditions
# --------------------------------------------------------------------------- #


def test_a_non_candidate_is_downgraded_rather_than_rejected() -> None:
    """WATCHLIST is a judgement, not a fault, and the status has to say which."""
    decision = decide(report=analyst_report(status=CandidateStatus.WATCHLIST))

    assert decision.reason is ForexRejection.NOT_A_CANDIDATE
    assert decision.status is ForexGateStatus.DOWNGRADED_WATCHLIST


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"state": account(capital_eur=None)}, ForexRejection.NO_CAPITAL),
        ({"state": account(rate=None)}, ForexRejection.FX_RATE_UNAVAILABLE),
        ({"market": market_context(instrument=None)}, ForexRejection.INSTRUMENT_UNRESOLVED),
        ({"market": market_context(atr_1h=None)}, ForexRejection.ATR_UNAVAILABLE),
        ({"market": market_context(candles_stale=True)}, ForexRejection.STALE_CANDLES),
    ],
)
def test_each_missing_precondition_has_its_own_code(
    kwargs: dict[str, object], expected: ForexRejection
) -> None:
    assert reason(**kwargs) is expected


def test_a_candidate_without_levels_is_named_as_such() -> None:
    """Distinct from NOT_A_CANDIDATE: the analyst *did* propose one and left it half-built."""
    report = analyst_report().model_copy(update={"stop": None})

    assert reason(report=report) is ForexRejection.MISSING_PLAN_FIELDS


def test_no_atr_refuses_rather_than_checking_against_nothing() -> None:
    """An unbounded stop is how a 35-pip idea becomes a 350-pip one with no symptom."""
    assert reason(market=market_context(atr_1h=None)) is ForexRejection.ATR_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Row 2 — the clock (§5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 8, 15, 12, 0, tzinfo=UTC), ForexRejection.MARKET_CLOSED),  # Saturday
        (datetime(2026, 8, 16, 21, 30, tzinfo=UTC), ForexRejection.WEEK_OPEN_QUIET),  # Sun open
        (datetime(2026, 8, 14, 19, 30, tzinfo=UTC), ForexRejection.FRIDAY_CUTOFF),  # Friday late
    ],
)
def test_the_clock_rail_names_which_part_of_the_week_it_is(
    now: datetime, expected: ForexRejection
) -> None:
    """Three different facts about the same calendar, and three different codes.

    §5.1 is explicit that closed is a **normal state** with its own reason rather than a
    degradation, and the other two are choices rather than facts — which is exactly why
    folding them into one code would make "how often is forex simply shut" unanswerable.
    """
    assert reason(now=now) is expected


def test_the_clock_rail_is_checked_before_anything_expensive() -> None:
    """A shut market rejects even a setup that is broken in four other ways.

    The ordering property, asserted on the case that distinguishes it: if geometry ran
    first this would report ENTRY_ZONE_INVALID, which is true and useless — nothing was
    ever going to be traded at 03:00 on a Saturday.
    """
    broken = analyst_report(zone=("1.20000", "1.10000"), stop="1.30000")

    assert (
        reason(report=broken, now=datetime(2026, 8, 15, 3, 0, tzinfo=UTC))
        is ForexRejection.MARKET_CLOSED
    )


# --------------------------------------------------------------------------- #
# Row 3 — the calendar (§8)
# --------------------------------------------------------------------------- #


def test_a_currency_matched_high_impact_event_blacks_the_pair_out() -> None:
    events = calendar(events=(high_impact(at=FX_NOW + timedelta(minutes=30), currency="USD"),))

    assert reason(events=events) is ForexRejection.EVENT_BLACKOUT


def test_an_event_in_an_unrelated_currency_does_not() -> None:
    """The non-vacuity sibling. EURUSD is exposed to EUR and USD, and to nothing else."""
    events = calendar(events=(high_impact(at=FX_NOW + timedelta(minutes=30), currency="JPY"),))

    assert decide(events=events).plan is not None


def test_a_calendar_with_no_coverage_suppresses_rather_than_degrades() -> None:
    """§8: with one source, "I do not know" and "nothing is scheduled" are one silence.

    This is the state the repository actually **ships** in, so it is also the reason
    forex emits nothing until the owner populates the file.
    """
    assert reason(events=calendar(covered=False)) is ForexRejection.CALENDAR_STALE


def test_the_shipped_calendar_is_the_unusable_one() -> None:
    """Named here rather than left as a surprise on switch-on day."""
    from sentinel.fx.calendar import load_calendar

    assert load_calendar().coverage_until is None


# --------------------------------------------------------------------------- #
# Row 4 — the spread (§5.3, defect #15)
# --------------------------------------------------------------------------- #


def test_a_spread_above_the_global_median_multiple_rejects(config: AppConfig) -> None:
    """EURUSD's global median is 1.1, so the 3.0x threshold is 3.3 pips."""
    wide = market_context(current_spread_pips="4.0")

    assert reason(market=wide) is ForexRejection.SPREAD_TOO_WIDE


def test_a_spread_just_under_the_threshold_passes() -> None:
    """The sibling that makes the test above mean something."""
    assert decide(market=market_context(current_spread_pips="3.3")).plan is not None


def test_the_per_hour_baseline_would_not_have_caught_the_rollover_hour() -> None:
    """Defect #15, kept visible rather than only fixed.

    §5.3 as written gated on the median **for that hour of day**. At 21:00 GBPUSD's
    hour-of-day median *is* 12 pips, so a 12-pip spread at 21:00 is "normal for that
    hour" and sails through — while costing 0.667R of an 18-pip stop. This computes
    both baselines against the same reading and asserts they disagree.
    """
    profile = spread_profile()
    at_21 = profile.expected_at(21)
    global_median = profile.global_median_pips

    assert at_21 > global_median, "the fixture must have a wide rollover hour to test this"
    # §5.3-as-written: normal for the hour, so it would pass.
    assert at_21 <= at_21 * Decimal("3.0")
    # What ships: measured against the global median, the same reading fails.
    assert Decimal("4.0") > global_median * Decimal("3.0")


def test_without_a_spread_series_the_clock_backstop_takes_over() -> None:
    """ "We cannot measure it" must not read as "it is fine" (§5.3)."""
    blind = market_context(current_spread_pips=None, profile=_empty_profile())

    assert reason(market=blind, now=FX_NOW.replace(hour=20)) is ForexRejection.ROLLOVER_WINDOW


def test_outside_the_rollover_window_a_blind_spread_still_passes() -> None:
    """The backstop is a clock window, not a blanket refusal."""
    blind = market_context(current_spread_pips=None, profile=_empty_profile())

    assert decide(market=blind).plan is not None


def _empty_profile() -> SpreadProfile:
    """Too few samples to be a baseline — ``spread_min_samples`` is 30."""
    return SpreadProfile(
        symbol="EURUSD",
        samples=2,
        global_median_pips=Decimal("1.1"),
        median_by_hour_pips={},
        min_pips=Decimal("1.0"),
        max_pips=Decimal("1.2"),
    )


# --------------------------------------------------------------------------- #
# Row 5 — geometry and quality
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("report_kwargs", "expected"),
    [
        ({"zone": ("1.16800", "1.16700")}, ForexRejection.ENTRY_ZONE_INVALID),
        ({"stop": "1.17000"}, ForexRejection.STOP_SIDE),
        ({"targets": ("1.18100", "1.17600", "1.18800")}, ForexRejection.TARGET_ORDER),
        ({"confidence": 40}, ForexRejection.LOW_CONFIDENCE),
    ],
)
def test_each_geometry_failure_has_its_own_code(
    report_kwargs: dict[str, object], expected: ForexRejection
) -> None:
    assert reason(report=analyst_report(**report_kwargs)) is expected  # type: ignore[arg-type]


def test_low_confidence_downgrades_and_does_not_reject() -> None:
    decision = decide(report=analyst_report(confidence=40))

    assert decision.status is ForexGateStatus.DOWNGRADED_WATCHLIST


def test_an_entry_further_than_half_a_percent_is_refused() -> None:
    """Spec defect #21's rail, on the number that made it necessary.

    1.1600 against a 1.16850 last price is 0.73% away — nothing in crypto's 3% bound and
    a long way in a market that moves 0.5% in a day.
    """
    far = analyst_report(zone=("1.15900", "1.16000"), stop="1.15600")

    assert reason(report=far) is ForexRejection.ENTRY_TOO_FAR


def test_the_same_entry_would_have_passed_cryptos_bound(config: AppConfig) -> None:
    """Which is the whole argument for #21, stated as an assertion."""
    distance_pct = (Decimal("1.16850") - Decimal("1.15900")) / Decimal("1.16850") * Decimal("100")

    assert distance_pct > config.forex.max_entry_distance_pct
    assert distance_pct < config.risk.max_entry_distance_pct


@pytest.mark.parametrize(
    ("stop", "atr", "expected"),
    [
        ("1.16690", "0.00150", ForexRejection.STOP_TOO_TIGHT),
        ("1.16000", "0.00100", ForexRejection.STOP_TOO_WIDE),
    ],
)
def test_the_stop_must_sit_inside_the_atr_band(
    stop: str, atr: str, expected: ForexRejection
) -> None:
    assert reason(report=analyst_report(stop=stop), market=market_context(atr_1h=atr)) is expected


def test_a_poor_gross_rr_is_named_before_costs_are_charged() -> None:
    """RR_TOO_LOW and NET_RR_TOO_LOW are different facts calling for different actions."""
    thin = analyst_report(targets=("1.16900", "1.17000", "1.17100"))

    assert reason(report=thin) is ForexRejection.RR_TOO_LOW


# --------------------------------------------------------------------------- #
# Row 6 — rails (§9)
# --------------------------------------------------------------------------- #


def test_one_open_forex_position_blocks_the_next(config: AppConfig) -> None:
    """§9's hard cap. All three pairs cross the dollar, so two positions is one bet."""
    assert config.forex.max_concurrent_positions == 1

    assert (
        reason(portfolio=ForexPortfolioState(open_positions=1))
        is ForexRejection.MAX_CONCURRENT_POSITIONS
    )


def test_a_pause_is_reported_ahead_of_every_other_rail() -> None:
    """What the person who typed /pause is looking for, even when other rails also hold."""
    busy = ForexPortfolioState(open_positions=1, signals_today=99, paused=True)

    assert reason(portfolio=busy) is ForexRejection.PAUSED


def test_the_daily_cap_is_three_not_cryptos_five(config: AppConfig) -> None:
    assert config.forex.max_signals_per_day == 3

    assert reason(portfolio=ForexPortfolioState(signals_today=3)) is ForexRejection.DAILY_SIGNAL_CAP


def test_a_symbol_inside_its_cooldown_is_refused() -> None:
    cooling = ForexPortfolioState(cooldown_until={"EURUSD": FX_NOW + timedelta(hours=1)})

    assert reason(portfolio=cooling) is ForexRejection.SYMBOL_COOLDOWN


def test_a_lapsed_cooldown_does_not_block() -> None:
    lapsed = ForexPortfolioState(cooldown_until={"EURUSD": FX_NOW - timedelta(hours=1)})

    assert decide(portfolio=lapsed).plan is not None


# --------------------------------------------------------------------------- #
# Row 7 — the ladder and the minimum ticket (§7.2, §16.3)
# --------------------------------------------------------------------------- #


def test_ten_thousand_euro_sizes_a_three_rung_ladder() -> None:
    decision = decide()

    assert decision.plan is not None
    assert len(decision.plan.entries) == 3
    assert [rung.weight_pct for rung in decision.plan.entries] == [
        Decimal("40"),
        Decimal("35"),
        Decimal("25"),
    ]


def test_at_two_hundred_euro_the_ticket_is_below_the_venue_minimum() -> None:
    """§16.3's consequence, and journal/M10b_REPORT.md §6's measurement, as a test.

    At €200 and 0.75% risk the whole position on a 35-pip stop is ~500 units against a
    1000-unit minimum — so not only does the ladder collapse, the single rung fails too.
    That is the ``BELOW_MIN_TICKET`` rate §7.2 asked to have measured, and it argues
    €200 is below the viable size for this market.
    """
    assert reason(state=account(capital_eur="200")) is ForexRejection.BELOW_MIN_TICKET


def test_the_capital_at_which_a_ladder_stops_collapsing_is_findable() -> None:
    """Between the two extremes there is a band where exactly one rung fits.

    Asserted as a *transition* rather than at a magic number: the point is that rung
    count falls monotonically with capital, which is what makes the count a reading on
    account size rather than an artefact.
    """
    counts = []
    for capital in ("200", "1000", "3000", "10000"):
        decision = decide(state=account(capital_eur=capital))
        counts.append(0 if decision.plan is None else len(decision.plan.entries))

    assert counts == sorted(counts), counts
    assert counts[0] == 0
    assert counts[-1] == 3


# --------------------------------------------------------------------------- #
# Row 8 — margin as a share of equity (§7.6)
# --------------------------------------------------------------------------- #


def test_margin_is_reported_as_a_share_of_equity_not_as_a_per_position_budget() -> None:
    decision = decide()

    assert decision.plan is not None
    assert decision.plan.margin_pct_of_equity > 0
    assert "ESMA" in decision.plan.leverage_basis


def test_a_position_needing_too_much_of_the_account_is_refused(config: AppConfig) -> None:
    """The rail is account-level (§7.6), so it is a share of equity and not a budget.

    A tight stop buys a large position: at a 6-pip stop the notional is big enough that
    30:1 margin passes the 20% ceiling.
    """
    tight = analyst_report(
        zone=("1.16800", "1.16820"),
        stop="1.16770",
        targets=("1.16990", "1.17070", "1.17150"),
    )

    # ATR 2 pips against a 4-pip stop — inside the 0.6-3.0 band, so the stop rails do
    # not fire and this test is about the margin rail rather than about the stop.
    assert (
        reason(report=tight, market=market_context(atr_1h="0.00020"))
        is ForexRejection.MARGIN_ABOVE_EQUITY_SHARE
    )


# --------------------------------------------------------------------------- #
# Row 9 — net RR, last (§7.4 as corrected by defect #16)
# --------------------------------------------------------------------------- #


#: A deliberately **single-rung** setup: the zone is 2 pips wide against a 15-pip ATR,
#: well under §3's ``0.5 x ATR`` threshold, so the weighted entry is exactly the zone
#: midpoint and the gross RR below is exactly 1.6 rather than whatever a three-rung
#: ladder happens to average to. The point of the pair is that **only the cost changes**,
#: and that is only demonstrable if the setup's own arithmetic is pinned.
MARGINAL_ENTRY = Decimal("1.16750")
MARGINAL_STOP = Decimal("1.16400")


def _marginal_report() -> AnalystReport:
    risk = MARGINAL_ENTRY - MARGINAL_STOP
    return analyst_report(
        zone=("1.16740", "1.16760"),
        stop=f"{MARGINAL_STOP:.5f}",
        targets=(
            f"{MARGINAL_ENTRY + risk * Decimal('1.6'):.5f}",
            f"{MARGINAL_ENTRY + risk * Decimal('2.0'):.5f}",
            f"{MARGINAL_ENTRY + risk * Decimal('2.4'):.5f}",
        ),
    )


def test_a_wide_spread_can_sink_a_setup_that_clears_the_gross_gate() -> None:
    """§7.4's whole point: any positive cost sinks a gross 1.5, and by more than it looks.

    A gross 1.6 on a 35-pip stop clears the gross gate comfortably. At a rollover-sized
    40-pip spread the same setup nets about 0.2R, because the spread widens the loss and
    shrinks the gain at once (defect #16).
    """
    rollover_hours = spread_profile()
    rollover_hours.median_by_hour_pips[12] = Decimal("40.0")

    assert (
        reason(report=_marginal_report(), market=market_context(profile=rollover_hours))
        is ForexRejection.NET_RR_TOO_LOW
    )


def test_the_same_setup_passes_at_the_normal_spread() -> None:
    """The sibling, and the reason the test above is about cost rather than about RR.

    Identical report, identical levels, identical gross 1.6. The only thing that moved
    is the spread this hour is priced at — 1.1 pips instead of 40 — and it is the
    difference between a signal and a rejection.
    """
    decision = decide(report=_marginal_report())

    assert decision.plan is not None, decision.message
    assert len(decision.plan.entries) == 1, "the pair rests on a single-rung ladder"
    assert decision.plan.rr_targets[0] == Decimal("1.60")


def test_net_rr_is_always_below_gross_because_the_spread_lands_on_both_sides() -> None:
    """Defect #16 as a property rather than as a number."""
    plan = decide().plan

    assert plan is not None
    assert all(net < gross for net, gross in zip(plan.rr_targets_net, plan.rr_targets, strict=True))


def test_the_cost_model_uses_this_hours_median_and_the_gate_used_the_global_one() -> None:
    """Defect #15's two baselines, both visible on one approved plan."""
    plan = decide().plan

    assert plan is not None
    assert plan.costs.spread_pips == spread_profile().expected_at(12)
    assert "12:00 UTC" in plan.costs.spread_basis


# --------------------------------------------------------------------------- #
# The yen cross, where §7.1's 145x error lives
# --------------------------------------------------------------------------- #


def test_usdjpy_sizes_through_eurjpy_and_a_hundredth_pip() -> None:
    """A pip of 0.01 and a rate of ~170, neither of which EURUSD's fixtures exercise."""
    report = analyst_report(
        symbol="USDJPY",
        zone=("150.100", "150.300"),
        stop="149.800",
        targets=("151.200", "151.800", "152.500"),
    )
    market = market_context(
        last_price="150.250", atr_1h="0.250", instrument=USDJPY, current_spread_pips="1.5"
    )
    decision = decide(report=report, market=market, state=account(rate="170.5"))

    assert decision.plan is not None, decision.message
    assert decision.plan.pip == Decimal("0.01")
    assert decision.plan.quote_currency == "JPY"
    assert decision.plan.eur_quote_rate == Decimal("170.5")


def test_sizing_usdjpy_at_the_eurusd_rate_would_look_unremarkable() -> None:
    """§7.1's named hazard, made visible.

    The wrong rate does not raise. It sizes the position ~145x smaller, which lands
    below the venue minimum and reports BELOW_MIN_TICKET — a rejection that reads like
    an ordinary small account rather than like a bug.
    """
    report = analyst_report(
        symbol="USDJPY",
        zone=("150.100", "150.300"),
        stop="149.800",
        targets=("151.200", "151.800", "152.500"),
    )
    market = market_context(
        last_price="150.250", atr_1h="0.250", instrument=USDJPY, current_spread_pips="1.5"
    )

    wrong = decide(report=report, market=market, state=account(rate="1.169"))

    assert wrong.reason is ForexRejection.BELOW_MIN_TICKET


# --------------------------------------------------------------------------- #
# The vocabularies
# --------------------------------------------------------------------------- #


def test_every_rejection_code_has_owner_facing_wording() -> None:
    """A rejection with no words is a card that says nothing — silence-as-success."""
    missing = [code.value for code in ForexRejection if code not in MESSAGES]

    assert not missing, missing


def test_the_two_markets_gate_statuses_agree_on_the_wire() -> None:
    """They are written to one column and read back by one /pulse (§16.4).

    Two vocabularies that disagreed would split one histogram in half with nothing to
    say so. This is why ``ForexGateStatus`` may be a separate enum but may not be a
    *different* one.
    """
    assert {status.value for status in ForexGateStatus} == {status.value for status in GateStatus}


def test_the_gate_records_the_prompt_version_on_every_verdict() -> None:
    """M9 compares by prompt_version, and a rejection is data too."""
    assert decide().prompt_version == "fable_forex_v1"
    assert decide(state=account(capital_eur=None)).prompt_version == "fable_forex_v1"


def test_a_swing_idea_late_in_the_week_carries_the_weekend_warning() -> None:
    """§5.4: the ladder expires before the close, and a position can still be open."""
    thursday = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    decision = decide(report=analyst_report(timeframe_label=TimeframeLabel.SWING), now=thursday)

    assert decision.plan is not None
    assert decision.plan.weekend_gap_warning is True
    assert "Friday" in decision.plan.expiry_basis


def test_a_short_is_gated_by_the_mirrored_geometry() -> None:
    """No test may pass by assuming a long."""
    short = analyst_report(
        direction=Direction.SHORT,
        zone=("1.16900", "1.17000"),
        stop="1.17300",
        targets=("1.16000", "1.15500", "1.14800"),
    )
    decision = decide(report=short)

    assert decision.plan is not None, decision.message
    assert decision.plan.direction is Direction.SHORT


def test_the_gate_is_a_pure_function_of_its_inputs(config: AppConfig) -> None:
    """Two identical calls, two identical plans but for the uuid. Nothing is cached."""
    from sentinel.core.clock import FrozenClock

    gate = ForexGate(config, calendar=calendar(), clock=FrozenClock(FX_NOW))
    first = gate.evaluate(
        report=analyst_report(),
        market=market_context(),
        account=account(),
        portfolio=ForexPortfolioState(),
    )
    second = gate.evaluate(
        report=analyst_report(),
        market=market_context(),
        account=account(),
        portfolio=ForexPortfolioState(),
    )
    assert first.plan is not None and second.plan is not None
    assert first.plan.model_dump(exclude={"plan_id"}) == second.plan.model_dump(exclude={"plan_id"})
