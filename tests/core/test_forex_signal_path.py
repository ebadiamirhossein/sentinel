"""The four joins M10b-2's boundary left untested by construction (FOREX.md §12).

**Read journal/M10b_2_REPORT.md §2 before this file.** M10b-1 shipped a Saxo adapter
whose output could not be drawn and all 1777 of its tests passed, because the adapter
was tested, the renderer was tested, and the *join* between them belonged to the next
session. The rule that came out of it is: **when a milestone boundary splits a producer
from its consumer, the next milestone composes them first and builds second.**

M10b-2 then named the four joins its own boundary with M10c leaves open:

1. a forex ``AnalystReport`` has never reached a card renderer;
2. a forex ``ChartRenderParams`` has never been persisted into ``signals.chart_params``;
3. ``ForexSizing`` has never been fed levels from a real analyst report — only from
   hand-computed fixtures;
4. ``TrackerLoop`` has never seen a forex symbol.

This file was written **before** ``sentinel/fx/plan.py``, ``sentinel/fx/gate.py`` and
``sentinel/bot/forex_cards.py`` existed, and it drives all four. Nothing below the HTTP
layer is mocked: the real Saxo adapter over a synthetic venue, the real feature engine,
the real renderer, the real gate, the real card and the real tracker loop.

**The levels are derived from the venue's own bars**, not written down here. That is
join 3: a hand-computed fixture proves the arithmetic and proves nothing about whether
an analyst report's levels survive a real ATR, a real spread profile and a real
instrument.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from sentinel.analyst.models import (
    AnalystReport,
    CandidateStatus,
    Direction,
    EntryZone,
    Evidence,
    SetupType,
    TimeframeLabel,
)
from sentinel.bot.forex_cards import forex_signal_card
from sentinel.bot.models import SignalRecord
from sentinel.charts.models import ChartSpec
from sentinel.charts.renderer import render_album
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings, load_config
from sentinel.core.forex_cycle import ForexAssembly, assemble_forex
from sentinel.core.markets import Market
from sentinel.fx.calendar import EconomicCalendar
from sentinel.fx.gate import (
    ForexAccountState,
    ForexGate,
    ForexMarketContext,
    ForexPortfolioState,
)
from sentinel.fx.models import ForexGateStatus
from sentinel.fx.plan import ForexPlan
from sentinel.tracker.loop import TrackerLoop
from sentinel.tracker.prices import PriceFeed
from tests.core.saxo_double import SyntheticSaxo, build
from tests.tracker_double import FakeDatabase, FakeStore, ScriptedFeed, fake_repositories

SYMBOL = "EURUSD"
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY"]
TZ = ZoneInfo("Europe/Vilnius")

#: Not the owner's €200. journal/M10b_REPORT.md §6 measured that at €200 the widest
#: EURUSD stop that sizes at all is 17.5 pips, so a composition test at that capital
#: would spend most of its life asserting BELOW_MIN_TICKET and would never once reach
#: a card. €200 gets its own test below, where the rejection is the point.
CAPITAL_EUR = Decimal("10000")

#: A calendar with coverage and no events. The shipped one claims **no** coverage, so
#: every gate call would reject with CALENDAR_STALE (§8) — correct behaviour, and it
#: would make this file prove nothing about anything downstream of the calendar.
OPEN_CALENDAR = EconomicCalendar(events=(), coverage_until=None, source="test")


async def assembly() -> ForexAssembly:
    transport = SyntheticSaxo()
    adapter, client = build(transport)
    async with client:
        return await assemble_forex(adapter, SYMBOLS, config=load_config(), now=transport.now)


def calendar_covering(now: datetime) -> EconomicCalendar:
    """Coverage well past ``now`` and nothing scheduled — the quiet-but-known case."""
    return EconomicCalendar(
        events=(), coverage_until=(now + timedelta(days=90)).date(), source="test"
    )


def context(assembly: ForexAssembly, symbol: str = SYMBOL) -> ForexMarketContext:
    """Everything the gate needs about the instrument, taken from the real cycle."""
    snapshot = next(s for s in assembly.snapshots if s.symbol == symbol)
    features = assembly.features[symbol]
    forex = assembly.forex_features[symbol]
    return ForexMarketContext(
        symbol=symbol,
        last_price=snapshot.last_price,
        atr_1h=features.timeframes["1h"].atr14,
        instrument=assembly.instruments[symbol],
        spread_profile=assembly.spread_profiles[symbol],
        current_spread_pips=forex.spread.current_pips if forex.spread else None,
    )


def report_for(market: ForexMarketContext, *, confidence: int = 78) -> AnalystReport:
    """A CANDIDATE whose levels are derived from the venue's own ATR — join 3.

    A long: the entry zone sits just under the last price, the stop 1.5 ATR below it,
    and the targets at roughly 3R / 5R / 7R. Wide targets on purpose — §7.4 says any
    positive cost sinks a gross 1.5, and this file is not the place to discover the
    net-RR gate; ``tests/fx/test_gate.py`` puts a setup on each side of it deliberately.
    """
    assert market.atr_1h is not None
    atr = market.atr_1h
    low = market.last_price - atr / Decimal(4)
    high = market.last_price - atr / Decimal(20)
    entry = (low + high) / Decimal(2)
    stop = entry - atr * Decimal("1.5")
    risk = entry - stop
    return AnalystReport(
        symbol=market.symbol,
        candidate_status=CandidateStatus.CANDIDATE,
        setup_type=SetupType.TREND_PULLBACK,
        direction=Direction.LONG,
        timeframe_label=TimeframeLabel.INTRADAY,
        thesis="Pullback into the prior-day open during the London-NY overlap.",
        evidence=(Evidence(claim="prior-day open holds", source_field="forex.prior_day.open"),),
        counter_thesis="The dollar index is firm and all three pairs cross the dollar.",
        entry_zone=EntryZone(low=low, high=high),
        stop=stop,
        targets=(entry + risk * 3, entry + risk * 5, entry + risk * 7),
        invalidation_price=stop,
        invalidation_text="1h close below the stop.",
        confidence=confidence,
        prompt_version="fable_forex_v1",
        model="claude-fable-5",
    )


def gate(config: AppConfig, now: datetime, calendar: EconomicCalendar | None = None) -> ForexGate:
    return ForexGate(config, calendar=calendar or calendar_covering(now), clock=FrozenClock(now))


def account(capital: Decimal = CAPITAL_EUR, rate: str = "1.169") -> ForexAccountState:
    return ForexAccountState(
        capital_eur=capital, risk_per_trade_pct=Decimal("0.75"), eur_quote_rate=Decimal(rate)
    )


@pytest.fixture
async def approved() -> tuple[ForexAssembly, ForexPlan]:
    """One approved forex plan, produced by the whole path. The fixture IS join 3."""
    built = await assembly()
    config = load_config()
    now = built.snapshots[0].captured_at
    market = context(built)
    decision = gate(config, now).evaluate(
        report=report_for(market),
        market=market,
        account=account(),
        portfolio=ForexPortfolioState(),
    )
    assert decision.plan is not None, f"the gate rejected the composition setup: {decision.message}"
    return built, decision.plan


# --------------------------------------------------------------------------- #
# Join 3 — ForexSizing fed by a real analyst report
# --------------------------------------------------------------------------- #


async def test_a_real_analyst_report_sizes_against_a_real_instrument(
    approved: tuple[ForexAssembly, ForexPlan],
) -> None:
    """Levels from a report, ATR from the feature engine, pip from reference data."""
    _, plan = approved

    assert plan.gate_status is ForexGateStatus.APPROVED_FOR_HUMAN
    assert plan.symbol == SYMBOL
    assert plan.quote_currency == "USD"
    assert plan.pip == Decimal("0.0001")
    assert plan.entries, "an approved plan with no rungs is not a plan"
    assert plan.risk_eur <= plan.planned_risk_eur, "never round up into more risk"
    assert plan.stop_distance_pips > 0
    assert plan.rr_targets_net[0] >= load_config().forex.min_rr_tp1


async def test_the_plan_has_no_field_for_a_concept_this_market_lacks() -> None:
    """§7.6 and §2.1 in their strong form — absent, not None and not zero."""
    forbidden = [
        name
        for name in ForexPlan.model_fields
        if "liq" in name or "funding" in name or name in {"notional_usdt", "suggested_leverage"}
    ]
    assert not forbidden, forbidden


# --------------------------------------------------------------------------- #
# Join 1 — a forex report reaches a card renderer
# --------------------------------------------------------------------------- #


async def test_a_forex_plan_renders_a_card(approved: tuple[ForexAssembly, ForexPlan]) -> None:
    """The seam that produced defect #12 in the first place."""
    _, plan = approved
    record = SignalRecord(plan=plan, user_id=7222549221, number=1, market=Market.FOREX)

    card = forex_signal_card(record, TZ, show_market=True)

    assert "FOREX · EURUSD" in card
    assert "pip" in card.lower()
    assert str(plan.stop) in card
    assert "USDT" not in card, "a forex card must never quote a crypto notional"
    assert "iq. buffer" not in card, "§7.6 forbids a liquidation buffer on this market"


# --------------------------------------------------------------------------- #
# Join 2 — a forex ChartRenderParams reaches signals.chart_params
# --------------------------------------------------------------------------- #


async def test_forex_chart_params_survive_a_round_trip_into_a_signal_row(
    approved: tuple[ForexAssembly, ForexPlan],
) -> None:
    """Decision #20's annotations, proved by a round trip rather than a unit test.

    They were introduced under a conditional serializer that omits the key when it is
    empty, and were verified by dumping the model. This asserts they survive the trip
    a stored chart actually takes.
    """
    built, plan = approved
    charts = render_album(
        next(s for s in built.snapshots if s.symbol == SYMBOL),
        built.features[SYMBOL],
        tuple(ChartSpec(symbol=SYMBOL, timeframe=tf) for tf in ("1h", "4h")),
        annotations=built.annotations[SYMBOL],
    )
    record = SignalRecord(
        plan=plan,
        user_id=7222549221,
        number=1,
        market=Market.FOREX,
        chart_params=tuple(chart.params.to_json_dict() for chart in charts),
    )

    store = FakeStore()
    row = store.add_signal(record)

    assert len(row.chart_params) == 2
    assert all(params["annotations"] for params in row.chart_params)
    assert row.market is Market.FOREX


# --------------------------------------------------------------------------- #
# Join 4 — TrackerLoop sees a forex symbol
# --------------------------------------------------------------------------- #


async def test_the_tracker_reads_a_forex_signal_row_back_as_a_forex_plan(
    approved: tuple[ForexAssembly, ForexPlan],
) -> None:
    """The loop rehydrates ``row.plan`` and must not read it through the crypto model."""
    _, plan = approved
    store = FakeStore()
    store.add_signal(SignalRecord(plan=plan, user_id=7222549221, number=1, market=Market.FOREX))
    settings = Settings(secrets=Secrets(_env_file=None), config=load_config())

    loop = TrackerLoop(
        FakeDatabase(store),  # type: ignore[arg-type]
        PriceFeed(ScriptedFeed(), settings.config.tracker),
        settings,
        market=Market.FOREX,
        clock=FrozenClock(plan.created_at),
        repositories=fake_repositories(),
    )
    result = await loop.tick()

    assert result.failures == [], result.failures
    assert result.checked == 1


# --------------------------------------------------------------------------- #
# The €200 account, where the rejection is the point (§7.2, §16.3)
# --------------------------------------------------------------------------- #


async def test_at_two_hundred_euro_the_ladder_collapses_or_the_ticket_is_refused() -> None:
    """§16.3's consequence, measured rather than assumed.

    At €200 a 40% rung is a few hundred units against a 1000-unit minimum, so either
    the ladder collapses to a single rung or the whole ticket is below the minimum.
    Both are correct; a three-rung forex ladder at this capital would not be.
    """
    built = await assembly()
    config = load_config()
    now = built.snapshots[0].captured_at
    market = context(built)

    decision = gate(config, now).evaluate(
        report=report_for(market),
        market=market,
        account=account(capital=Decimal("200")),
        portfolio=ForexPortfolioState(),
    )

    if decision.plan is None:
        assert decision.reason is not None and decision.reason.value == "BELOW_MIN_TICKET"
    else:
        assert len(decision.plan.entries) == 1, "no three-rung forex ladder fits €200"
