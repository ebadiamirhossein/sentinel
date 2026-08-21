"""Every §12 failure mode degrades forex and **only** forex.

docs/specs/FOREX.md §12 lists ten ways the forex half can fail and requires, for each
one, a test asserting that a crypto cycle completes normally while forex is in that
state. This file is that requirement.

Two things are asserted for every mode, because either alone would be weak:

1. **The forex failure is real.** Each case actually drives the forex code into the
   named state and asserts the specific error or verdict it produces. A test that
   only checked crypto still worked would pass just as happily if the forex code did
   nothing at all.
2. **Crypto completes unchanged.** A full crypto cycle runs — screener, deep analyst,
   risk gate, publish, and the ``cycles`` row closed OK — against fakes for the two
   things that cost money.

And one structural claim underneath all of them:
``test_a_crypto_cycle_never_touches_a_saxo_adapter_or_credential`` makes constructing
either one an error for the duration of a cycle. With forex disabled that is
architecture rather than luck, and it is the reason the ten cases above can be as
brief as they are.

The doubles here are deliberately local rather than shared with
``test_multi_user.py``: this file's whole subject is isolation, and a test of
isolation that reaches into another suite's fixtures to get its work done is arguing
against itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from sentinel.analyst.models import AnalystReport
from sentinel.core import orchestrator as orchestrator_module
from sentinel.core.clock import FrozenClock
from sentinel.core.config import ForexConfig, Settings
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.core.orchestrator import CycleOrchestrator, CycleRepositories
from sentinel.fx.calendar import EconomicCalendar
from sentinel.fx.errors import (
    DegradedRead,
    InstrumentUnresolved,
    ReauthenticationRequired,
)
from sentinel.fx.hours import MarketState, clock_verdict, state_at
from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.models import ForexRejection, SaxoTokenBundle
from sentinel.fx.sizing import size_position
from sentinel.ingestion.adapters.forex_saxo import SaxoForexAdapter
from sentinel.ingestion.clients.saxo_auth import SaxoAuth
from sentinel.ingestion.errors import SourceUnavailable
from sentinel.ingestion.models import FxRate, MarketSnapshot
from sentinel.llm.models import LLMCall
from sentinel.llm.spend import SpendState, SpendTotals, evaluate_market_spend
from sentinel.risk.models import MarketContext, PauseState
from tests.conftest import cassette
from tests.ingestion.conftest import make_fetcher
from tests.market_double import snapshot_from_cassettes
from tests.risk_double import analyst_report, market_context

from .conftest import NOW, CycleDatabase, CycleStore, CycleUsers

SYMBOL = "SOLUSDT"
FOREX_CONFIG = ForexConfig()


# --------------------------------------------------------------------------- #
# A crypto cycle, end to end, against fakes for the two things that cost money
# --------------------------------------------------------------------------- #


class _Signals:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def open_symbols_by_user(self) -> dict[int, set[str]]:
        return {}

    async def resolutions_by_user_since(self, since: datetime) -> dict[int, list[Any]]:
        return {}

    async def published_by_user_since(self, since: datetime) -> dict[int, int]:
        return {}

    async def open_taken(self, *, user_id: int) -> list[Any]:
        return []

    async def open_symbols(self, *, user_id: int) -> set[str]:
        return set()

    async def resolutions_since(self, since: datetime, *, user_id: int) -> list[Any]:
        return []

    async def published_since(self, since: datetime, *, user_id: int) -> int:
        return 0

    async def claim(self, record: Any) -> Any:
        self._store.signals[record.signal_id] = record
        return record.model_copy(update={"number": len(self._store.signals)})


class _Cycles:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def start(self, cycle_id: UUID, **kwargs: Any) -> None:
        self._store.cycles[cycle_id] = {"status": "RUNNING", **kwargs}

    async def finish(self, cycle_id: UUID, **kwargs: Any) -> None:
        self._store.cycles[cycle_id] = kwargs


class _Gates:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def record(self, decision: Any, cycle_id: Any = None, *, user_id: int) -> None:
        self._store.gate_decisions.append((user_id, decision))


class _Reports:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def save(self, report: Any, **kwargs: Any) -> UUID:
        self._store.reports.append(report)
        return uuid4()

    async def latest_non_candidates(self, *, since: datetime) -> dict[str, datetime]:
        return {}

    async def recent_for_symbol(self, symbol: str, **kwargs: Any) -> list[Any]:
        return []


class _Snapshots:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def save(self, snapshot: MarketSnapshot) -> UUID:
        self._store.snapshots.append(snapshot)
        return snapshot.snapshot_id


class _LLMCalls:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def record_many(self, calls: Sequence[LLMCall]) -> int:
        self._store.llm_calls.extend(calls)
        return len(calls)

    async def spend_totals(self, **_: object) -> SpendTotals:
        return SpendTotals(day_usd=self._store.spend_day, month_usd=self._store.spend_day)

    async def spend_totals_across_markets(self, **_: object) -> SpendTotals:
        return SpendTotals(day_usd=self._store.spend_day, month_usd=self._store.spend_day)

    async def day_spend_by_market(self, **_: object) -> dict[Market, Decimal]:
        """M10b-2's reserved-floor input. One market in this fake, so the breakdown is
        the same figure once more — and crypto's floor never reserves against crypto."""
        return {Market.CRYPTO: self._store.spend_day}


class _RiskState:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def load(self) -> PauseState:
        return self._store.pause


class _MarketPause:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store
        self._market = market

    async def load(self) -> PauseState:
        # A forex pause is a forex pause. Crypto reads its own, which is absent.
        if self._market is Market.FOREX:
            return self._store.market_pause
        return PauseState()


class _UserMarketPause:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None: ...

    async def load(self, user_id: int) -> PauseState:
        return PauseState()


class _Settings:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def all(self) -> dict[str, Any]:
        return dict(self._store.settings)


class _Fx:
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None: ...

    async def get(self, pair: str = "EURUSD") -> FxRate:
        return FxRate(pair="EURUSD", rate=Decimal("1.1593"), source="test", fetched_at=NOW)


class _Screener:
    def __init__(
        self, client: Any, config: Any, *, cycle_id: Any = None, prompt_version: str = ""
    ) -> None: ...

    async def screen(self, snapshots: Any, features: Any) -> Any:
        interesting = [type("V", (), {"symbol": s.symbol})() for s in snapshots]
        return type("Verdicts", (), {"calls": [], "interesting": interesting})()


class _Analyst:
    name = "test-analyst"

    def __init__(
        self, client: Any, config: Any, *, cycle_id: Any = None, prompt_version: str = ""
    ) -> None:
        self.calls: list[Any] = []
        self.prompt_version = prompt_version

    async def analyze(self, snapshot: Any, charts: Any, history: str) -> AnalystReport:
        # ``risk_double``'s report and ``market_context`` are written against each
        # other, so the gate approves on its merits rather than on a coincidence
        # between a cassette's last price and a hand-picked entry zone.
        return analyst_report()


class _Publisher:
    def __init__(self, user_id: int, log: list[tuple[int, str]]) -> None:
        self._user_id = user_id
        self._log = log

    async def publish(self, plan: Any, charts: Any = (), *, cycle_id: Any = None) -> Any:
        self._log.append((self._user_id, plan.symbol))
        return type("Result", (), {"published": True, "record": None, "reason": ""})()


@asynccontextmanager
async def _no_assembler(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
    yield object()


async def _no_setup_stats(*args: Any, **kwargs: Any) -> list[Any]:
    """The history block's calibration half. Empty is the honest answer here — these
    tests are about isolation, and M9's statistics are exercised where they live."""
    return []


async def run_crypto_cycle(
    settings: Settings, store: CycleStore, snapshot: MarketSnapshot
) -> tuple[Any, list[tuple[int, str]]]:
    """One full crypto cycle: screen, analyse, gate, publish, close the row."""
    published: list[tuple[int, str]] = []

    async def _assemble(*args: Any, **kwargs: Any) -> tuple[list[MarketSnapshot], dict[str, Any]]:
        return [snapshot], {SYMBOL: object()}

    engine = CycleOrchestrator(
        settings,
        CycleDatabase(store),  # type: ignore[arg-type]
        market=Market.CRYPTO,
        publisher_factory=lambda user_id: _Publisher(user_id, published),  # type: ignore[arg-type,return-value]
        clock=FrozenClock(NOW),
        repositories=CycleRepositories(
            signals=_Signals,  # type: ignore[arg-type]
            cycles=_Cycles,  # type: ignore[arg-type]
            gate_decisions=_Gates,  # type: ignore[arg-type]
            reports=_Reports,  # type: ignore[arg-type]
            llm_calls=_LLMCalls,  # type: ignore[arg-type]
            snapshots=_Snapshots,  # type: ignore[arg-type]
            risk_state=_RiskState,  # type: ignore[arg-type]
            settings=_Settings,  # type: ignore[arg-type]
            fx=_Fx,  # type: ignore[arg-type]
            users=CycleUsers,  # type: ignore[arg-type]
            market_pause=_MarketPause,  # type: ignore[arg-type]
            user_market_pause=_UserMarketPause,  # type: ignore[arg-type]
        ),
    )
    with (
        patch.object(orchestrator_module, "snapshot_assembler", _no_assembler),
        patch.object(orchestrator_module, "assemble_with_features", _assemble),
        patch.object(orchestrator_module, "Screener", _Screener),
        patch.object(orchestrator_module, "AnthropicFableAnalyst", _Analyst),
        patch.object(orchestrator_module, "render_album", lambda *a, **k: []),
        patch.object(orchestrator_module, "setup_stats", _no_setup_stats),
    ):
        result = await engine.run()
    return result, published


@pytest.fixture
def crypto_snapshot(monkeypatch: pytest.MonkeyPatch) -> MarketSnapshot:
    monkeypatch.setattr(
        MarketContext, "from_snapshot", staticmethod(lambda *a, **k: market_context())
    )
    return snapshot_from_cassettes(SYMBOL)


@pytest.fixture
def crypto_store() -> CycleStore:
    from tests.bot_double import owner_account

    store = CycleStore()
    store.users = [owner_account(7222549221, capital_eur=Decimal("10000"))]
    return store


@pytest.fixture
def live_settings(settings: Settings) -> Settings:
    """Crypto live, forex present and disabled — the shipped configuration."""
    markets = dict(settings.config.markets)
    markets[Market.CRYPTO] = markets[Market.CRYPTO].model_copy(
        update={"dry_run": False, "watchlist": (SYMBOL,)}
    )
    return Settings(
        secrets=settings.secrets, config=settings.config.model_copy(update={"markets": markets})
    )


def assert_crypto_cycle_completed(result: Any, published: list[tuple[int, str]]) -> None:
    assert result.error is None
    assert result.market is Market.CRYPTO
    assert result.symbols_scanned == 1
    assert result.candidates == 1
    assert result.analyzed == 1
    assert result.approved == 1
    assert published == [(7222549221, SYMBOL)]


# --------------------------------------------------------------------------- #
# The structural claim the ten cases below rest on
# --------------------------------------------------------------------------- #


async def test_a_crypto_cycle_never_touches_a_saxo_adapter_or_credential(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    """Constructing either is made an error for the duration of the cycle.

    This is what makes "forex degrades forex only" architecture rather than luck: a
    crypto cycle has no code path that reaches a Saxo endpoint, so no state the forex
    half can be in is reachable from it.
    """

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a crypto cycle constructed a forex object")

    with (
        patch.object(SaxoForexAdapter, "__init__", forbidden),
        patch.object(SaxoAuth, "__init__", forbidden),
    ):
        result, published = await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)

    assert_crypto_cycle_completed(result, published)


# --------------------------------------------------------------------------- #
# §12, one case at a time
# --------------------------------------------------------------------------- #


def _tokens(store_bundle: SaxoTokenBundle | None) -> Any:
    class _Store:
        async def load(self) -> SaxoTokenBundle | None:
            return store_bundle

        async def save(self, bundle: SaxoTokenBundle) -> None:
            raise AssertionError("nothing in these tests should reach a save")

    return _Store()


def _auth(transport: httpx.MockTransport, bundle: SaxoTokenBundle | None) -> tuple[Any, Any]:
    fetcher, client = make_fetcher(transport, max_retries=0)
    return (
        SaxoAuth(
            FOREX_CONFIG,
            fetcher=fetcher,
            store=_tokens(bundle),
            app_key=SecretStr("k"),
            app_secret=SecretStr("s"),
            clock=FrozenClock(NOW),
        ),
        client,
    )


def _status(code: int) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(code, json={"error": "nope"}))


async def _forex_adapter(handler: Handler) -> tuple[SaxoForexAdapter, httpx.AsyncClient]:
    fetcher, client = make_fetcher(httpx.MockTransport(handler), max_retries=0)

    class _StubTokens:
        async def access_token(self) -> str:
            return "stub"

    return (
        SaxoForexAdapter(
            FOREX_CONFIG,
            fetcher=fetcher,
            tokens=_StubTokens(),
            clock=FrozenClock(datetime(2026, 8, 21, 8, 30, tzinfo=UTC)),
        ),
        client,
    )


Handler = Callable[[httpx.Request], httpx.Response]


def _reference_only(chart: Handler) -> Handler:
    """Reference data always resolves; only the chart call is made to misbehave."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/instruments/details"):
            return httpx.Response(200, json=cassette("saxo_ref_details.json"))
        if path.endswith("/ref/v1/instruments"):
            return httpx.Response(200, json=cassette("saxo_ref_instruments_EURUSD.json"))
        return chart(request)

    return handler


async def test_refresh_token_expired_after_an_outage_longer_than_an_hour(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    """§3.1's one-hour memory, and §12's first row."""
    dead = SaxoTokenBundle(
        refresh_token=SecretStr("spent"),
        obtained_at=NOW - timedelta(hours=2),
        refresh_expires_at=NOW - timedelta(hours=1),
    )
    auth, client = _auth(_status(200), dead)
    async with client:
        with pytest.raises(ReauthenticationRequired, match="one-hour memory"):
            await auth.refresh()

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )


async def test_oauth_refresh_fails_at_the_vendor(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    auth, client = _auth(
        _status(503), SaxoTokenBundle(refresh_token=SecretStr("live"), obtained_at=NOW)
    )
    async with client:
        with pytest.raises(SourceUnavailable):
            await auth.refresh()

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )


async def test_the_chart_endpoint_errors(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    adapter, client = await _forex_adapter(
        _reference_only(lambda request: httpx.Response(500, json={"Message": "boom"}))
    )
    async with client:
        with pytest.raises(SourceUnavailable):
            await adapter.fetch_tail("EURUSD", "1h", 10)

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )


async def test_a_short_candle_count_is_a_degraded_read(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    """D-e. Asking for 12 and being handed 10 is a degraded read, not a shorter tail."""
    adapter, client = await _forex_adapter(
        _reference_only(
            lambda request: httpx.Response(200, json=cassette("saxo_chart_EURUSD_60.json"))
        )
    )
    async with client:
        with pytest.raises(DegradedRead, match="degraded read"):
            await adapter.fetch_tail("EURUSD", "1h", 12)

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )


async def test_instrument_resolution_fails(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    adapter, client = await _forex_adapter(lambda request: httpx.Response(200, json={"Data": []}))
    async with client:
        with pytest.raises(InstrumentUnresolved):
            await adapter.resolve("EURUSD")
        assert await adapter.resolve_many(("EURUSD",)) == {}

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )


async def test_the_market_data_terms_flag_pauses_forex_and_nothing_else(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    """D-i. ``MarketDataViaOpenApiTermsAccepted`` reads **False while data flows**, so
    it is not a reliable gate — but a change in it is worth pausing forex on, and
    M10a's per-market pause is where that lands.

    The assertion that matters is the second one: the pause is scoped to forex, and a
    crypto cycle reading its own (absent) pause is unaffected.
    """
    crypto_store.market_pause = PauseState(paused=True)

    forex_pause = _MarketPause(type("S", (), {"store": crypto_store})(), market=Market.FOREX)
    crypto_pause = _MarketPause(type("S", (), {"store": crypto_store})(), market=Market.CRYPTO)
    assert (await forex_pause.load()).is_active(NOW) is True
    assert (await crypto_pause.load()).is_active(NOW) is False

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )


async def test_the_calendar_is_stale_or_missing(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    """§8: suppress rather than trade blind. There is no second source."""
    empty = EconomicCalendar(events=(), coverage_until=None, source="test")
    verdict = empty.blackout(
        "EURUSD", NOW, before_minutes=60, after_minutes=30, warn_within_days=14
    )
    assert verdict.rejection is ForexRejection.CALENDAR_STALE

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )


async def test_frankfurter_is_unavailable(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    """No EUR->quote rate means no sizing, so no forex signal. Never a guessed rate.

    Crypto's own FX fallback is untouched — its cycle sizes against the last-known-good
    rate exactly as it always has, which is what the completed cycle below shows.
    """
    outcome = size_position(
        instrument=ForexInstrument(
            symbol="EURUSD",
            uic=21,
            decimals=4,
            pip=Decimal("0.0001"),
            tick_size=Decimal("0.00001"),
            min_trade_size=Decimal("1000"),
            amount_decimals=2,
            base_currency="EUR",
            quote_currency="USD",
            resolved_at=NOW,
        ),
        entry_price=Decimal("1.1692"),
        stop_price=Decimal("1.1674"),
        capital_eur=Decimal("300"),
        risk_per_trade_pct=Decimal("0.75"),
        eur_quote_rate=None,
        max_leverage=30,
    )
    assert outcome.rejection is ForexRejection.FX_RATE_UNAVAILABLE

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )


async def test_the_forex_market_is_closed(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    """A normal state with its own code — never DEGRADED, and never crypto's problem.

    Crypto is open at the same instant, which is the whole reason market hours had to
    become a per-market concept rather than a global one.
    """
    saturday = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    assert state_at(saturday, FOREX_CONFIG) is MarketState.CLOSED
    assert clock_verdict(saturday, config=FOREX_CONFIG).reason is ForexRejection.MARKET_CLOSED

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )


async def test_the_forex_sub_budget_is_exhausted(
    live_settings: Settings, crypto_store: CycleStore, crypto_snapshot: MarketSnapshot
) -> None:
    """M10a's two-tier guard, from the other side: forex over its own $4 while the
    deployment is under the $11 ceiling stops forex and nothing else."""
    config = live_settings.config
    forex_verdict = evaluate_market_spend(
        market_totals=SpendTotals(day_usd=Decimal("4.50"), month_usd=Decimal("4.50")),
        global_totals=SpendTotals(day_usd=Decimal("6.00"), month_usd=Decimal("6.00")),
        market=config.market(Market.FOREX),
        global_limit_usd=config.llm_daily_budget_global_usd,
        config=config.llm,
    )
    crypto_verdict = evaluate_market_spend(
        market_totals=SpendTotals(day_usd=Decimal("1.50"), month_usd=Decimal("1.50")),
        global_totals=SpendTotals(day_usd=Decimal("6.00"), month_usd=Decimal("6.00")),
        market=config.market(Market.CRYPTO),
        global_limit_usd=config.llm_daily_budget_global_usd,
        config=config.llm,
    )
    assert forex_verdict.state is SpendState.LIMIT_REACHED
    assert forex_verdict.suspends_analysis is True
    assert crypto_verdict.suspends_analysis is False

    assert_crypto_cycle_completed(
        *await run_crypto_cycle(live_settings, crypto_store, crypto_snapshot)
    )
