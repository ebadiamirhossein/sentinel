"""One analysis per cycle, shared; sizing, rails and cards per user (M8.1).

This is the design constraint the whole milestone rests on, so it is asserted
rather than described:

* the deep analyst runs **once per symbol per cycle**, whoever is approved — it is
  the ~$0.32 tier and its output is a judgment about a market, not about a person;
* the gate runs **once per eligible user**, against that user's own capital, risk %
  and rails, and writes a ``gate_decisions`` row that says whose verdict it is;
* nobody eligible ⇒ **the analyst is not called at all**, because nobody should pay
  for a plan with no recipient;
* the pre-analyst guard is the **union**: one member's cooldown must not suppress a
  shared analysis for everybody;
* a user who is set up but has no capital is still evaluated, rejected with
  ``NO_CAPITAL``, and **told why** — once a day, not once a cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from sentinel.analyst.models import AnalystReport
from sentinel.bot.models import SignalRecord, UserStatus
from sentinel.core import orchestrator as orchestrator_module
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Settings
from sentinel.core.orchestrator import (
    CycleOrchestrator,
    CycleRepositories,
    CycleResult,
    SkipReason,
)
from sentinel.ingestion.models import FxRate, MarketSnapshot
from sentinel.risk.models import MarketContext, PauseReason, PauseState, RejectionReason
from tests.bot_double import member_account, owner_account
from tests.market_double import snapshot_from_cassettes
from tests.risk_double import analyst_report, market_context

from .conftest import NOW, CycleDatabase, CycleStore, CycleUsers

OWNER = 111
MEMBER = 222
SYMBOL = "SOLUSDT"


# --------------------------------------------------------------------------- #
# Fakes: only what the fan-out touches
# --------------------------------------------------------------------------- #


class Signals:
    def __init__(self, session: Any) -> None:
        self._store: CycleStore = session.store

    # -- per-user reads, all required-keyword by design ---------------------- #

    async def open_taken(self, *, user_id: int) -> list[Any]:
        return []

    async def open_symbols(self, *, user_id: int) -> set[str]:
        return self._store.open_by_user.get(user_id, set())

    async def resolutions_since(
        self, since: datetime, *, user_id: int
    ) -> list[tuple[str, datetime]]:
        return self._store.cooldowns_by_user.get(user_id, [])

    async def published_since(self, since: datetime, *, user_id: int) -> int:
        return self._store.today_by_user.get(user_id, 0)

    # -- the grouped reads the union guard makes ----------------------------- #

    async def open_symbols_by_user(self) -> dict[int, set[str]]:
        return dict(self._store.open_by_user)

    async def resolutions_by_user_since(
        self, since: datetime
    ) -> dict[int, list[tuple[str, datetime]]]:
        return dict(self._store.cooldowns_by_user)

    async def published_by_user_since(self, since: datetime) -> dict[int, int]:
        return dict(self._store.today_by_user)

    async def claim(self, record: SignalRecord) -> SignalRecord | None:
        self._store.signals[record.signal_id] = record
        return record.model_copy(update={"number": len(self._store.signals)})


class Gates:
    def __init__(self, session: Any) -> None:
        self._store: CycleStore = session.store

    async def record(self, decision: Any, cycle_id: Any = None, *, user_id: int) -> None:
        self._store.gate_decisions.append((user_id, decision))


class RiskState:
    def __init__(self, session: Any) -> None:
        self._store: CycleStore = session.store

    async def load(self) -> PauseState:
        return self._store.pause


class Fx:
    def __init__(self, session: Any) -> None: ...

    async def get(self, pair: str = "EURUSD") -> FxRate:
        return FxRate(pair="EURUSD", rate=Decimal("1.1593"), source="test", fetched_at=NOW)


class Settings_:
    def __init__(self, session: Any) -> None:
        self._store: CycleStore = session.store

    async def all(self) -> dict[str, Any]:
        return dict(self._store.settings)


class Publisher:
    """One per user, as the orchestrator builds them. Records who got what."""

    def __init__(self, user_id: int, log: list[tuple[int, str]]) -> None:
        self._user_id = user_id
        self._log = log

    async def publish(self, plan: Any, charts: Any = (), *, cycle_id: Any = None) -> Any:
        self._log.append((self._user_id, plan.symbol))
        return type("Result", (), {"published": True, "record": None, "reason": ""})()


class Notices:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def notice(self, user_id: int, *, key: str, text: str) -> bool:
        self.sent.append((user_id, key))
        return True


@pytest.fixture
def sol(monkeypatch: pytest.MonkeyPatch) -> MarketSnapshot:
    """A SOLUSDT snapshot, with the market facts the §8.1 golden report was written
    against.

    ``MarketContext.from_snapshot`` is stubbed to ``risk_double.market_context()``
    for one reason: these tests are about the fan-out, and the snapshot-to-context
    conversion is M1/M4's and is asserted there. Leaving it live would make every
    assertion below depend on a cassette's last price happening to sit within 3% of
    a fixture's entry zone — a coincidence, not a guarantee.
    """
    monkeypatch.setattr(
        MarketContext, "from_snapshot", staticmethod(lambda *args, **kwargs: market_context())
    )
    return snapshot_from_cassettes("SOLUSDT")


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The repo config ships ``dry_run: true`` (it is the safe default a fresh
    install boots with). These tests are about what is *published*, so they run
    against a live config; the rehearsal case makes its own dry copy."""
    return Settings(
        secrets=settings.secrets, config=settings.config.model_copy(update={"dry_run": False})
    )


@pytest.fixture
def store() -> CycleStore:
    store = CycleStore()
    store.open_by_user = {}
    store.cooldowns_by_user = {}
    store.today_by_user = {}
    return store


def repositories() -> CycleRepositories:
    return CycleRepositories(
        signals=Signals,  # type: ignore[arg-type]
        gate_decisions=Gates,  # type: ignore[arg-type]
        risk_state=RiskState,  # type: ignore[arg-type]
        settings=Settings_,  # type: ignore[arg-type]
        fx=Fx,  # type: ignore[arg-type]
        users=CycleUsers,  # type: ignore[arg-type]
    )


def orchestrator(
    settings: Settings,
    store: CycleStore,
    *,
    published: list[tuple[int, str]] | None = None,
    notices: Notices | None = None,
) -> CycleOrchestrator:
    log = published if published is not None else []
    return CycleOrchestrator(
        settings,
        CycleDatabase(store),  # type: ignore[arg-type]
        publisher_factory=lambda user_id: Publisher(user_id, log),  # type: ignore[arg-type,return-value]
        notices=notices,  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        repositories=repositories(),
    )


def report() -> AnalystReport:
    return analyst_report()


# --------------------------------------------------------------------------- #
# Who a cycle fans out to
# --------------------------------------------------------------------------- #


async def test_only_approved_acknowledged_unpaused_users_are_recipients(
    settings: Settings, store: CycleStore
) -> None:
    later = NOW + timedelta(hours=1)
    store.users = [
        owner_account(OWNER, capital_eur=Decimal("10000")),
        member_account(MEMBER, capital_eur=Decimal("5000")),
        member_account(301, status=UserStatus.PENDING, acknowledged=False),
        member_account(302, status=UserStatus.SUSPENDED),
        member_account(303, status=UserStatus.LEFT),
        member_account(304, acknowledged=False),
        member_account(
            305,
            pause=PauseState(paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=later),
        ),
    ]
    recipients = await orchestrator(settings, store)._recipients(now=NOW)

    assert [user.telegram_user_id for user in recipients] == [OWNER, MEMBER]


async def test_a_lapsed_loss_pause_lets_a_user_back_in(
    settings: Settings, store: CycleStore
) -> None:
    """The 24h window expires on its own — no ``/resume`` required, and members
    have no ``/resume`` to run."""
    lapsed = PauseState(paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=NOW)
    store.users = [member_account(MEMBER, capital_eur=Decimal("5000"), pause=lapsed)]

    recipients = await orchestrator(settings, store)._recipients(now=NOW)

    assert [user.telegram_user_id for user in recipients] == [MEMBER]


async def test_a_user_with_no_capital_is_still_a_recipient(
    settings: Settings, store: CycleStore
) -> None:
    """So the gate can reject them with ``NO_CAPITAL`` and the notice can say why.
    What their missing capital does gate is whether an *analyst call* is made."""
    store.users = [member_account(MEMBER)]
    recipients = await orchestrator(settings, store)._recipients(now=NOW)
    assert [user.telegram_user_id for user in recipients] == [MEMBER]


# --------------------------------------------------------------------------- #
# The union guard, before the expensive tier
# --------------------------------------------------------------------------- #


async def test_one_users_cooldown_does_not_suppress_the_analysis_for_everybody(
    settings: Settings, store: CycleStore
) -> None:
    """The whole reason the pre-analyst guard runs on the union.

    The analysis is shared. If the owner's four-hour cooldown on SOLUSDT could stop
    it being analysed, the cheapest guard in the system would become its most
    expensive mistake — a member would silently never hear about that symbol again.
    """
    owner = owner_account(OWNER, capital_eur=Decimal("10000"))
    member = member_account(MEMBER, capital_eur=Decimal("5000"))
    store.cooldowns_by_user = {OWNER: [(SYMBOL, NOW)]}
    result = CycleResult(cycle_id=uuid4())

    allowed = await orchestrator(settings, store)._allowed(
        {SYMBOL}, result, started=NOW, config=settings.config, recipients=[owner, member]
    )

    assert allowed == {SYMBOL}
    assert result.skipped == {}


async def test_a_symbol_nobody_can_receive_is_dropped_before_the_analyst(
    settings: Settings, store: CycleStore
) -> None:
    owner = owner_account(OWNER, capital_eur=Decimal("10000"))
    member = member_account(MEMBER, capital_eur=Decimal("5000"))
    store.open_by_user = {OWNER: {SYMBOL}, MEMBER: {SYMBOL}}
    result = CycleResult(cycle_id=uuid4())

    allowed = await orchestrator(settings, store)._allowed(
        {SYMBOL}, result, started=NOW, config=settings.config, recipients=[owner, member]
    )

    assert allowed == set()
    assert result.skipped[SYMBOL].reason is SkipReason.OPEN_SIGNAL


async def test_nobody_funded_means_nothing_is_analysed(
    settings: Settings, store: CycleStore
) -> None:
    """An analyst call is ~$0.32. A plan with no possible recipient is not worth one."""
    result = CycleResult(cycle_id=uuid4())

    allowed = await orchestrator(settings, store)._allowed(
        {SYMBOL}, result, started=NOW, config=settings.config, recipients=[member_account(MEMBER)]
    )

    assert allowed == set()
    assert result.skipped[SYMBOL].reason is SkipReason.NO_FUNDED_USER
    assert result.skipped[SYMBOL].detail == "no user is set up to receive a signal"


# --------------------------------------------------------------------------- #
# The fan-out itself
# --------------------------------------------------------------------------- #


async def test_one_shared_report_produces_one_sized_plan_per_user(
    settings: Settings, store: CycleStore, sol: MarketSnapshot
) -> None:
    """Two users, two capitals, two plans — from one analysis.

    The euro figures must differ (that is what per-user sizing means) and the
    ``gate_decisions`` rows must say whose verdict each one is.
    """
    store.users = [
        owner_account(OWNER, capital_eur=Decimal("10000")),
        member_account(MEMBER, capital_eur=Decimal("2000")),
    ]
    published: list[tuple[int, str]] = []

    await orchestrator(settings, store, published=published)._gate_and_publish(
        CycleResult(cycle_id=uuid4()),
        report=report(),
        snapshot=sol,
        stored={},
        charts=[],
        recipients=store.users,
    )

    assert [user for user, _ in published] == [OWNER, MEMBER]
    assert [user for user, _ in store.gate_decisions] == [OWNER, MEMBER]

    plans = [decision.plan for _, decision in store.gate_decisions]
    assert all(plan is not None for plan in plans)
    assert plans[0].capital_eur == Decimal("10000")
    assert plans[1].capital_eur == Decimal("2000")
    assert plans[0].risk_eur != plans[1].risk_eur, "one capital, one plan — not a broadcast"


async def test_each_users_own_risk_percentage_sizes_their_plan(
    settings: Settings, store: CycleStore, sol: MarketSnapshot
) -> None:
    """Same capital, different risk %: the two plans must not be the same plan."""
    users = [
        owner_account(OWNER, capital_eur=Decimal("10000"), risk_per_trade_pct=Decimal("0.5")),
        member_account(MEMBER, capital_eur=Decimal("10000"), risk_per_trade_pct=Decimal("1.5")),
    ]
    store.users = users

    await orchestrator(settings, store)._gate_and_publish(
        CycleResult(cycle_id=uuid4()),
        report=report(),
        snapshot=sol,
        stored={},
        charts=[],
        recipients=users,
    )

    owner_plan, member_plan = (decision.plan for _, decision in store.gate_decisions)
    assert owner_plan.risk_per_trade_pct == Decimal("0.5")
    assert member_plan.risk_per_trade_pct == Decimal("1.5")
    assert member_plan.risk_eur > owner_plan.risk_eur


async def test_a_user_without_capital_is_rejected_and_told_why_once_a_day(
    settings: Settings, store: CycleStore, sol: MarketSnapshot
) -> None:
    notices = Notices()
    users = [member_account(MEMBER)]
    store.users = users
    published: list[tuple[int, str]] = []
    engine = orchestrator(settings, store, published=published, notices=notices)

    for _ in range(3):  # three cycles in one UTC day
        await engine._gate_and_publish(
            CycleResult(cycle_id=uuid4()),
            report=report(),
            snapshot=sol,
            stored={},
            charts=[],
            recipients=users,
        )

    assert published == [], "no card can be sized, so none is sent"
    reasons = {decision.reason for _, decision in store.gate_decisions}
    assert reasons == {RejectionReason.NO_CAPITAL}
    assert [user for user, _ in notices.sent] == [MEMBER, MEMBER, MEMBER]
    assert len({key for _, key in notices.sent}) == 1, (
        "one key per user per UTC day — the notifier's claim makes it one message"
    )


async def test_a_user_already_holding_the_symbol_is_skipped_without_a_gate_row(
    settings: Settings, store: CycleStore, sol: MarketSnapshot
) -> None:
    """PRD F11's "max 1 active signal per symbol", per user.

    The gate has no dedup rail of its own, so the pure guard is applied again inside
    the fan-out. Nothing was evaluated, so there is no verdict to store.
    """
    users = [
        owner_account(OWNER, capital_eur=Decimal("10000")),
        member_account(MEMBER, capital_eur=Decimal("5000")),
    ]
    store.users = users
    store.open_by_user = {OWNER: {SYMBOL}}
    published: list[tuple[int, str]] = []

    await orchestrator(settings, store, published=published)._gate_and_publish(
        CycleResult(cycle_id=uuid4()),
        report=report(),
        snapshot=sol,
        stored={},
        charts=[],
        recipients=users,
    )

    assert [user for user, _ in published] == [MEMBER]
    assert [user for user, _ in store.gate_decisions] == [MEMBER]


async def test_the_operator_pause_holds_back_every_user(
    settings: Settings, store: CycleStore, sol: MarketSnapshot
) -> None:
    """``/pause`` is system-wide and owner-only; it is the wider statement and it
    wins over any individual's state."""
    users = [
        owner_account(OWNER, capital_eur=Decimal("10000")),
        member_account(MEMBER, capital_eur=Decimal("5000")),
    ]
    store.users = users
    store.pause = PauseState(paused=True, reason=PauseReason.MANUAL, until=None)
    published: list[tuple[int, str]] = []

    await orchestrator(settings, store, published=published)._gate_and_publish(
        CycleResult(cycle_id=uuid4()),
        report=report(),
        snapshot=sol,
        stored={},
        charts=[],
        recipients=users,
    )

    assert published == []
    assert {decision.reason for _, decision in store.gate_decisions} == {RejectionReason.PAUSED}


async def test_a_dry_run_cycle_rehearses_the_fan_out(
    settings: Settings, store: CycleStore, sol: MarketSnapshot
) -> None:
    """Each user gets their own stored row, sized against their own capital, and
    nothing reaches Telegram. A rehearsal that only rehearsed one user would not
    rehearse this milestone at all."""
    dry = Settings(
        secrets=settings.secrets, config=settings.config.model_copy(update={"dry_run": True})
    )
    users = [
        owner_account(OWNER, capital_eur=Decimal("10000")),
        member_account(MEMBER, capital_eur=Decimal("2000")),
    ]
    store.users = users
    published: list[tuple[int, str]] = []

    await orchestrator(dry, store, published=published)._gate_and_publish(
        CycleResult(cycle_id=uuid4()),
        report=report(),
        snapshot=sol,
        stored={},
        charts=[],
        recipients=users,
    )

    assert published == [], "a rehearsal publishes nothing"
    stored = list(store.signals.values())
    assert sorted(record.user_id for record in stored) == [OWNER, MEMBER]
    assert all(record.dry_run for record in stored)


# --------------------------------------------------------------------------- #
# The analyst runs once, whoever is approved
# --------------------------------------------------------------------------- #


class CountingAnalyst:
    """Counts how many times a deep analysis is asked for.

    ``calls`` is the LLM-call list the orchestrator collects, so the counter needs a
    different name — ``analyses``.
    """

    name = "counting"
    analyses = 0

    def __init__(self, client: Any, config: Any, *, cycle_id: Any) -> None:
        self.calls: list[Any] = []

    async def analyze(self, snapshot: Any, charts: Any, history: str) -> AnalystReport:
        CountingAnalyst.analyses += 1
        return report()


class Reports:
    def __init__(self, session: Any) -> None:
        self._store: CycleStore = session.store

    async def save(self, report_: Any, **kwargs: Any) -> UUID:
        self._store.reports.append(report_)
        return uuid4()


async def test_the_deep_analyst_is_called_once_no_matter_how_many_users(
    settings: Settings, store: CycleStore, sol: MarketSnapshot
) -> None:
    """The design constraint, asserted at the only place it can be broken.

    Four approved users, one symbol, one cycle: **one** analyst call. Running the
    analyst per user would multiply a ~$0.32 call by the number of friends and
    produce four different judgments about the same market.
    """
    users = [
        owner_account(OWNER, capital_eur=Decimal("10000")),
        member_account(MEMBER, capital_eur=Decimal("5000")),
        member_account(333, capital_eur=Decimal("3000")),
        member_account(444, capital_eur=Decimal("1500")),
    ]
    store.users = users
    published: list[tuple[int, str]] = []
    result = CycleResult(cycle_id=uuid4())
    CountingAnalyst.analyses = 0

    repos = repositories()
    engine = CycleOrchestrator(
        settings,
        CycleDatabase(store),  # type: ignore[arg-type]
        publisher_factory=lambda user_id: Publisher(user_id, published),  # type: ignore[arg-type,return-value]
        clock=FrozenClock(NOW),
        repositories=CycleRepositories(**{**repos.__dict__, "reports": Reports}),
    )

    with (
        patch.object(orchestrator_module, "AnthropicFableAnalyst", CountingAnalyst),
        patch.object(orchestrator_module, "render_album", lambda *a, **k: []),
    ):
        await engine._analyse_symbol(
            result,
            snapshot=sol,
            features={SYMBOL: object()},  # type: ignore[dict-item]
            stored={},
            client=None,  # type: ignore[arg-type]
            recipients=users,
            owner_id=None,
        )

    assert CountingAnalyst.analyses == 1, "one analysis per symbol per cycle, always"
    assert len(store.reports) == 1, "and one analyst_reports row, not one per user"
    assert result.analyzed == 1
    assert len(published) == 4, "but four cards, one per user"
    assert sorted(user for user, _ in published) == [OWNER, MEMBER, 333, 444]


async def test_the_history_block_is_the_owners_book(settings: Settings, store: CycleStore) -> None:
    """specs/PROMPTS.md §3, owner-scoped (M8.1 owner ruling).

    One shared analysis now produces one signal row per user. Counting all of them
    would multiply the sample size by the number of friends and make the win rate a
    weighted average of everybody's execution — a number that changes when somebody
    new joins, which is not a fact about the market.
    """
    asked: dict[str, Any] = {}

    class Reports_:
        def __init__(self, session: Any) -> None: ...

        async def recent_for_symbol(
            self, symbol: str, limit: int = 3, *, owner_id: int, role: str = "primary"
        ) -> list[Any]:
            asked["verdicts_owner"] = owner_id
            return []

    async def fake_setup_stats(
        session: Any, *, now: datetime, owner_id: int, **kw: Any
    ) -> list[Any]:
        asked["stats_owner"] = owner_id
        return []

    repos = repositories()
    engine = CycleOrchestrator(
        settings,
        CycleDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        repositories=CycleRepositories(**{**repos.__dict__, "reports": Reports_}),
    )

    with patch.object(orchestrator_module, "setup_stats", fake_setup_stats):
        block = await engine._history_block(SYMBOL, owner_id=OWNER)

    assert asked == {"verdicts_owner": OWNER, "stats_owner": OWNER}
    assert SYMBOL in block


async def test_without_an_owner_the_history_block_degrades_rather_than_borrowing(
    settings: Settings, store: CycleStore
) -> None:
    """A fresh deployment has no owner row yet. The block must say "nothing measured"
    rather than quietly calibrating the analyst on somebody else's numbers."""
    block = await orchestrator(settings, store)._history_block(SYMBOL, owner_id=None)

    assert "has not been deeply analyzed before" in block
    assert "no outcomes resolved yet" in block


def test_the_owner_id_falls_back_to_a_single_entry_allowlist() -> None:
    """So an existing single-id ``.env`` keeps working untouched, and a multi-id one
    — which predates roles entirely — declines to guess."""
    from sentinel.core.config import Secrets

    assert Secrets(_env_file=None, telegram_allowed_user_ids="111").owner_user_id == 111
    assert Secrets(_env_file=None, telegram_allowed_user_ids="111,222").owner_user_id is None
    assert (
        Secrets(
            _env_file=None, telegram_owner_user_id=999, telegram_allowed_user_ids="111,222"
        ).owner_user_id
        == 999
    )


def test_the_frozen_clock_is_inside_the_plans_lifetime() -> None:
    """Guard on the fixtures themselves: a report whose entry zone is stale would
    make every gate assertion above pass for the wrong reason."""
    assert datetime(2026, 8, 18, 12, 0, tzinfo=UTC) == NOW
