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

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import UUID
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
from sentinel.core import orchestrator as orchestrator_module
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings, load_config
from sentinel.core.forex_cycle import ForexAssembly, assemble_forex
from sentinel.core.markets import Market
from sentinel.core.orchestrator import CycleOrchestrator, CycleRepositories
from sentinel.fx.calendar import EconomicCalendar
from sentinel.fx.gate import (
    ForexAccountState,
    ForexGate,
    ForexMarketContext,
    ForexPortfolioState,
)
from sentinel.fx.models import ForexGateStatus
from sentinel.fx.plan import ForexPlan
from sentinel.ingestion.models import FxRate, MarketSnapshot
from sentinel.llm.spend import SpendTotals
from sentinel.risk.models import PauseState
from sentinel.tracker.loop import TrackerLoop
from sentinel.tracker.prices import PriceFeed
from tests.bot_double import owner_account
from tests.core.conftest import CycleDatabase, CycleStore, CycleUsers
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


# --------------------------------------------------------------------------- #
# The whole cycle, through the real orchestrator
# --------------------------------------------------------------------------- #
#
# The four tests above compose the pieces by hand. This one runs the code that will
# actually run: ``CycleOrchestrator._run_forex_cycle``, over the real Saxo adapter, the
# real feature engine, the real renderer, the real gate and the real publisher seam.
#
# The doubles are local to this file rather than borrowed from
# ``test_forex_degrades_only.py``. That file's subject is isolation and this one's is
# composition, and a composition test that reached into another suite for its plumbing
# would be proving something about the test suite instead.


class _Repo:
    """Base for the repository doubles — every one of them is market-scoped."""

    def __init__(self, session: Any, *, market: Market = Market.CRYPTO) -> None:
        self.store: CycleStore = session.store
        self.market = market


class _Signals(_Repo):
    async def open_symbols_by_user(self) -> dict[int, set[str]]:
        return {}

    async def open_symbols(self, *, user_id: int) -> set[str]:
        return set()

    async def resolutions_since(self, since: datetime, *, user_id: int) -> list[Any]:
        return []

    async def published_since(self, since: datetime, *, user_id: int) -> int:
        return 0

    async def open_taken(self, *, user_id: int) -> list[Any]:
        return []

    async def claim(self, record: Any) -> Any:
        self.store.signals[record.signal_id] = record
        return record.model_copy(update={"number": len(self.store.signals)})


class _Cycles(_Repo):
    async def start(self, cycle_id: UUID, **kwargs: Any) -> None:
        self.store.cycles[cycle_id] = {"status": "RUNNING", **kwargs}

    async def finish(self, cycle_id: UUID, **kwargs: Any) -> None:
        self.store.cycles[cycle_id] = kwargs


class _Gates(_Repo):
    async def record(self, decision: Any, cycle_id: Any = None, *, user_id: int) -> None:
        self.store.gate_decisions.append(decision)


class _Reports(_Repo):
    async def save(self, report: Any, **kwargs: Any) -> UUID:
        self.store.reports.append(report)
        return UUID(int=len(self.store.reports))

    async def latest_non_candidates(self, *, since: datetime) -> dict[str, datetime]:
        return {}

    async def recent_for_symbol(self, symbol: str, **kwargs: Any) -> list[Any]:
        return []


class _Snapshots(_Repo):
    async def save(self, snapshot: MarketSnapshot) -> UUID:
        self.store.snapshots.append(snapshot)
        return snapshot.snapshot_id


class _LLMCalls(_Repo):
    async def record_many(self, calls: Any) -> int:
        self.store.llm_calls.extend(calls)
        return len(calls)

    async def spend_totals(self, **_: object) -> SpendTotals:
        return SpendTotals(day_usd=Decimal("0"), month_usd=Decimal("0"))

    async def spend_totals_across_markets(self, **_: object) -> SpendTotals:
        return SpendTotals(day_usd=Decimal("0"), month_usd=Decimal("0"))

    async def day_spend_by_market(self, **_: object) -> dict[Market, Decimal]:
        return {}


class _Pause(_Repo):
    async def load(self, user_id: int | None = None) -> PauseState:
        return PauseState()


class _SettingsRepo(_Repo):
    async def all(self) -> dict[str, Any]:
        return {}


class _FxRates(_Repo):
    """``fx_rates``, keyed by pair — which is what makes EURJPY expressible."""

    def __init__(self, session: Any, *, market: Market = Market.CRYPTO) -> None:
        super().__init__(session, market=market)
        self.rates: dict[str, FxRate] = session.store.fx_rates

    async def get(self, pair: str = "EURUSD") -> FxRate | None:
        return self.rates.get(pair)

    async def upsert(self, rate: FxRate) -> None:
        self.rates[rate.pair] = rate


class _FxClient:
    """Frankfurter, scripted. One EUR-based request, several currencies back."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, ...]] = []

    async def fetch_quotes(self, currencies: tuple[str, ...]) -> dict[str, FxRate]:
        self.asked.append(currencies)
        table = {"USD": Decimal("1.169"), "JPY": Decimal("170.5"), "GBP": Decimal("0.845")}
        return {
            f"EUR{currency}": FxRate(
                pair=f"EUR{currency}",
                rate=table[currency],
                source="test",
                fetched_at=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
            )
            for currency in currencies
            if currency in table
        }


class _ForexAnalyst:
    """Returns a CANDIDATE whose levels are derived from the snapshot it was given.

    This is join 3 again, one level up: the levels are not written down here, so they
    survive whatever the feature engine actually computed for the bars the adapter
    actually fetched.
    """

    name = "test-forex-analyst"

    #: Every history block this analyst was handed, so the cycle test can assert on
    #: what the model would actually have received (M10d, join 4).
    histories: list[str] = []

    def __init__(
        self, client: Any, config: Any, *, cycle_id: Any = None, prompt_version: str = ""
    ) -> None:
        self.calls: list[Any] = []
        self.prompt_version = prompt_version

    async def analyze(self, snapshot: Any, charts: Any, history: str) -> AnalystReport:
        _ForexAnalyst.histories.append(history)
        # ``snapshot.features`` carries the forex block merged in beside the per-
        # timeframe ones (M10b-2), so it is deliberately NOT a ``SymbolFeatures``
        # payload. Reading the ATR out of the raw dict is what a consumer of this
        # snapshot actually has to do.
        atr = Decimal(str(snapshot.features["timeframes"]["1h"]["atr14"]))
        market = ForexMarketContext(
            symbol=snapshot.symbol, last_price=snapshot.last_price, atr_1h=atr
        )
        return report_for(market)


class _Publisher:
    def __init__(self, user_id: int, log: list[Any], tz: Any) -> None:
        self._user_id = user_id
        self._log = log
        self._tz = tz

    async def publish(self, plan: Any, charts: Any = (), *, cycle_id: Any = None) -> Any:
        record = SignalRecord(plan=plan, user_id=self._user_id, number=1, market=Market.FOREX)
        # The publisher's own renderer is dispatched on market; calling the forex one
        # here is the same dispatch, and it is what makes this assert on a real card.
        self._log.append((self._user_id, plan.symbol, forex_signal_card(record, self._tz)))
        return type("Result", (), {"published": True, "record": record, "reason": ""})()


def _forex_settings(config: AppConfig) -> Settings:
    """Forex enabled, live, three pairs — the switch-on shape, in memory only."""
    markets = dict(config.markets)
    markets[Market.FOREX] = markets[Market.FOREX].model_copy(
        update={"enabled": True, "dry_run": False}
    )
    return Settings(
        secrets=Secrets(_env_file=None, ANTHROPIC_API_KEY="test-key"),
        config=config.model_copy(update={"markets": markets}),
    )


async def _no_setup_stats(*args: Any, **kwargs: Any) -> list[Any]:
    """No resolved outcomes yet, which is what a fresh forex book actually has."""
    return []


async def run_forex_cycle(
    config: AppConfig, *, calendar: EconomicCalendar, store: CycleStore | None = None
) -> tuple[Any, list[Any], CycleStore, _FxClient]:
    store = store or CycleStore()
    store.users = [owner_account(7222549221, capital_eur=CAPITAL_EUR)]
    published: list[Any] = []
    transport = SyntheticSaxo()
    fx_client = _FxClient()
    tz = ZoneInfo("Europe/Vilnius")

    @asynccontextmanager
    async def _adapter(*args: Any, **kwargs: Any) -> Any:
        adapter, client = build(transport)
        async with client:
            yield adapter

    engine = CycleOrchestrator(
        _forex_settings(config),
        CycleDatabase(store),  # type: ignore[arg-type]
        market=Market.FOREX,
        publisher_factory=lambda uid: _Publisher(uid, published, tz),  # type: ignore[arg-type,return-value]
        clock=FrozenClock(transport.now),
        calendar=calendar,
        fx_client=fx_client,  # type: ignore[arg-type]
        repositories=CycleRepositories(
            signals=_Signals,  # type: ignore[arg-type]
            cycles=_Cycles,  # type: ignore[arg-type]
            gate_decisions=_Gates,  # type: ignore[arg-type]
            reports=_Reports,  # type: ignore[arg-type]
            llm_calls=_LLMCalls,  # type: ignore[arg-type]
            snapshots=_Snapshots,  # type: ignore[arg-type]
            risk_state=_Pause,  # type: ignore[arg-type]
            settings=_SettingsRepo,  # type: ignore[arg-type]
            fx=_FxRates,  # type: ignore[arg-type]
            users=CycleUsers,  # type: ignore[arg-type]
            market_pause=_Pause,  # type: ignore[arg-type]
            user_market_pause=_Pause,  # type: ignore[arg-type]
        ),
    )
    with (
        patch.object(orchestrator_module, "forex_adapter", _adapter),
        patch.object(orchestrator_module, "AnthropicFableAnalyst", _ForexAnalyst),
        # specs/PROMPTS.md §3's calibration block, which the forex path started
        # building at M10d — it passed a literal "" until then, and an empty text block
        # is an HTTP 400 rather than an empty section (journal/M10d_REPORT.md, join 4).
        # `setup_stats` runs a real SELECT, and this fake session answers every read
        # with "no rows" rather than with a result object, exactly as
        # tests/core/test_forex_degrades_only.py already patches it.
        patch.object(orchestrator_module, "setup_stats", _no_setup_stats),
    ):
        result = await engine.run()
    return result, published, store, fx_client


async def test_the_forex_analyst_is_never_handed_an_empty_history_block() -> None:
    """The cycle-level half of journal/M10d_REPORT.md's join-4 defect.

    ``user_blocks`` now drops an empty text block, so a ``""`` here would no longer
    400 — it would silently send the analyst no calibration context at all, which is
    the quieter and worse version of the same bug. specs/PROMPTS.md §3's block is the
    model's only view of its own measured performance, and ``build_history_block``
    states "nothing measured yet" **in words** rather than by saying nothing.

    Asserted on what the analyst was handed rather than on the request, because the
    request is where the 400 was and the point is that the input was wrong before it
    ever got there.
    """
    _ForexAnalyst.histories = []
    config = load_config()
    now = SyntheticSaxo().now
    await run_forex_cycle(config, calendar=calendar_covering(now))

    assert _ForexAnalyst.histories, "no analyst call was made at all"
    for history in _ForexAnalyst.histories:
        assert history.strip(), "an empty history block is an HTTP 400, not an empty section"
        assert "RECENT PIPELINE HISTORY" in history


async def test_a_full_forex_cycle_reaches_a_published_card() -> None:
    """The whole path, through the code that will actually run it.

    M10b's ``cycle.forex_stops_at_report`` is gone, and this is what replaced it: three
    symbols ingested from a synthetic venue, features computed, charts rendered, an
    analyst asked, a gate run per user, and a card produced.
    """
    config = load_config()
    now = SyntheticSaxo().now
    result, published, _store, _ = await run_forex_cycle(config, calendar=calendar_covering(now))

    assert result.error is None
    assert result.market is Market.FOREX
    assert result.symbols_scanned == 3
    assert result.analyzed == 3
    assert result.candidates == 3
    assert result.approved >= 1
    assert published, "a forex cycle that approves a plan must publish it"

    _, symbol, card = published[0]
    assert symbol in SYMBOLS
    assert "pips" in card
    assert "USDT" not in card


async def test_the_cycle_fetches_the_euro_rate_for_every_quote_currency() -> None:
    """§7.1: USDJPY sizes through EURJPY, so one request must cover USD **and** JPY."""
    config = load_config()
    now = SyntheticSaxo().now
    _, _, store, fx_client = await run_forex_cycle(config, calendar=calendar_covering(now))

    assert fx_client.asked == [("JPY", "USD")]
    assert set(store.fx_rates) == {"EURUSD", "EURJPY"}


async def test_the_usdjpy_plan_is_sized_at_the_yen_rate_not_the_dollar_one() -> None:
    """The ~145x error §7.1 names, asserted absent on a plan the real cycle produced."""
    config = load_config()
    now = SyntheticSaxo().now
    _, _, store, _ = await run_forex_cycle(config, calendar=calendar_covering(now))

    plans = [d.plan for d in store.gate_decisions if d.plan is not None]
    yen = next((plan for plan in plans if plan.symbol == "USDJPY"), None)
    if yen is None:
        pytest.skip("USDJPY did not clear the gate on this synthetic data")
    assert yen.eur_quote_rate == Decimal("170.5")
    assert yen.quote_currency == "JPY"
    assert yen.pip == Decimal("0.01")


async def test_a_stale_calendar_stops_the_whole_cycle_short_of_a_signal() -> None:
    """§8, at cycle scale: with one source, silence is not "nothing is scheduled".

    And this is the state the repository **ships** in, so it is also the answer to
    "what happens on the day forex is switched on before the calendar is populated".
    """
    config = load_config()
    result, published, store, _ = await run_forex_cycle(config, calendar=OPEN_CALENDAR)

    assert result.analyzed == 3
    assert published == []
    assert store.gate_decisions
    assert all(d.reason.value == "CALENDAR_STALE" for d in store.gate_decisions)


async def test_every_gate_verdict_is_recorded_whether_or_not_it_approved() -> None:
    """PRD F10: a rejection is data. M9 cannot compare what was never written down."""
    config = load_config()
    now = SyntheticSaxo().now
    _, _, store, _ = await run_forex_cycle(config, calendar=calendar_covering(now))

    assert len(store.gate_decisions) == 3
    assert all(d.prompt_version == "fable_forex_v1" for d in store.gate_decisions)
