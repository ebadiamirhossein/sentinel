"""Fixtures for the cycle orchestrator suite.

The orchestrator is the one module that touches everything, so its tests drive it
against fakes for the two things that cost money — the Anthropic API and Telegram
— and a real risk engine, real cards and a real feature engine underneath. A test
that faked the gate would prove the orchestrator calls something, not that it
ships the plan the engine approved.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.models import SignalRecord, UserAccount, UserStatus
from sentinel.core.config import Secrets, Settings, load_config
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.risk.models import PauseState

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def tz() -> ZoneInfo:
    return ZoneInfo("Europe/Vilnius")


@pytest.fixture
def plan() -> Any:
    """A real gate-approved plan — never a hand-built one (M6's rule)."""
    from tests.risk_double import approved_plan

    return approved_plan(load_config())


@pytest.fixture
def settings() -> Settings:
    secrets = Secrets(_env_file=None, ANTHROPIC_API_KEY="test-key")
    return Settings(secrets=secrets, config=load_config())


@dataclass
class CycleStore:
    """The slice of Postgres a cycle reads and writes."""

    settings: dict[str, Any] = field(default_factory=dict)
    pause: PauseState = field(default_factory=PauseState)
    signals: dict[UUID, Any] = field(default_factory=dict)
    cycles: dict[UUID, Any] = field(default_factory=dict)
    snapshots: list[Any] = field(default_factory=list)
    reports: list[Any] = field(default_factory=list)
    gate_decisions: list[Any] = field(default_factory=list)
    llm_calls: list[Any] = field(default_factory=list)
    spend_day: Decimal = Decimal("0")
    open_symbols: set[str] = field(default_factory=set)
    resolutions: list[tuple[str, datetime]] = field(default_factory=list)
    published_today: int = 0
    #: M10a — this market's per-market operator pause, and the per-user one. Both
    #: default to absent, i.e. not paused; ``pause`` above is still the global rail.
    market_pause: PauseState = field(default_factory=PauseState)
    #: M8.1 — who the cycle fans out to. Empty means nobody is set up, and the
    #: orchestrator must then skip the deep analyst entirely.
    users: list[UserAccount] = field(default_factory=list)
    #: ``fx_rates``, keyed by pair (M10c). Keyed rather than a single figure because
    #: forex sizes in EUR against the **quote** currency, which is EURJPY on the yen
    #: cross (§7.1) — a table with one row could not express that.
    fx_rates: dict[str, Any] = field(default_factory=dict)
    #: The same three guard inputs as above, partitioned by user — which is what the
    #: union guard and the per-user dedup check actually read from M8.1.
    open_by_user: dict[int, set[str]] = field(default_factory=dict)
    cooldowns_by_user: dict[int, list[tuple[str, datetime]]] = field(default_factory=dict)
    today_by_user: dict[int, int] = field(default_factory=dict)
    #: M8.2 — ``{symbol: created_at}`` for the newest WATCHLIST/NO_SETUP verdict per
    #: symbol, i.e. what the re-analysis cooldown reads. Empty means "nothing has
    #: been analysed recently", which is the right default for every other test.
    non_candidates: dict[str, datetime] = field(default_factory=dict)
    committed: int = 0


class _EmptyRows:
    """What the fake session returns for a SELECT: no rows.

    The real ``AnalystReportRepository`` is used unmocked by several cycle tests —
    only ``save()`` mattered until M8.2 added a query. Rather than make every one of
    those tests wire a double they do not care about, the fake session answers reads
    honestly: this database has no analyst_reports rows. Tests that exercise the
    re-analysis rail override ``reports`` with a double backed by
    ``CycleStore.non_candidates``.
    """

    def all(self) -> list[Any]:
        return []

    def scalars(self) -> _EmptyRows:
        return self

    def first(self) -> None:
        return None


class CycleSession:
    def __init__(self, store: CycleStore) -> None:
        self.store = store

    async def execute(self, *args: Any, **kwargs: Any) -> _EmptyRows:
        return _EmptyRows()

    async def get(self, *args: Any, **kwargs: Any) -> None:
        """A primary-key lookup that finds nothing.

        M10a's two pause tables are read this way, and the honest answer for this
        fake database is that neither has a row — i.e. nothing is paused, which is
        what every test that does not set a pause is entitled to assume. A test that
        cares overrides the repository, exactly as the ``reports`` reads do.
        """
        return None

    async def commit(self) -> None:
        self.store.committed += 1

    async def rollback(self) -> None:  # pragma: no cover
        pass


class CycleDatabase:
    def __init__(self, store: CycleStore | None = None) -> None:
        self.store = store or CycleStore()

    def session(self) -> Any:
        @asynccontextmanager
        async def _session() -> Any:
            yield CycleSession(self.store)

        return _session()


class CycleUsers:
    """``UserRepository``'s read half, from ``CycleStore.users`` (M8.1)."""

    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def get(self, user_id: int) -> UserAccount | None:
        return next((u for u in self._store.users if u.telegram_user_id == user_id), None)

    async def owner(self) -> UserAccount | None:
        return next((u for u in self._store.users if u.is_owner), None)

    async def all(self) -> list[UserAccount]:
        return list(self._store.users)

    async def approved(self) -> list[UserAccount]:
        return sorted(
            (u for u in self._store.users if u.status is UserStatus.APPROVED),
            key=lambda u: u.telegram_user_id,
        )


def rehearsing(settings: Settings, market: Market = LEGACY_MARKET) -> Settings:
    """The same settings with one market's ``dry_run`` on (M10a).

    ``dry_run`` moved from the top of ``AppConfig`` onto each market, because the
    point of a second market is that it can rehearse for a fortnight while the first
    keeps posting real cards. One helper, so every test that rehearses does it the
    same way and none of them re-implements the nesting.
    """
    market_config = settings.config.market(market).model_copy(update={"dry_run": True})
    return Settings(
        secrets=settings.secrets,
        config=settings.config.model_copy(
            update={"markets": {**settings.config.markets, market: market_config}}
        ),
    )


def signal_row(record: SignalRecord) -> Any:
    """A ``SignalRow``-shaped object for the store, without a database."""
    plan = record.plan
    return type(
        "Row",
        (),
        {
            "id": record.signal_id,
            "plan_id": plan.plan_id,
            "number": record.number or 1,
            "user_id": record.user_id,
            "symbol": plan.symbol,
            "plan": plan.model_dump(mode="json"),
            "status": record.status.value,
            "decision": None,
            "dry_run": record.dry_run,
            "created_at": plan.created_at,
            "expires_at": plan.expires_at,
        },
    )()


__all__ = [
    "NOW",
    "CycleDatabase",
    "CycleSession",
    "CycleStore",
    "CycleUsers",
    "rehearsing",
    "signal_row",
]
