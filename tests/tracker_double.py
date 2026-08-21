"""An in-memory stand-in for the tracker's slice of Postgres.

Same argument as ``tests/bot_double.py``: crash recovery and event idempotency are
the headline guarantees of this milestone, and testing them only when a developer
happens to have ``SENTINEL_TEST_DATABASE_URL`` set would leave them effectively
untested.

The fakes enforce the two unique constraints that *carry* the guarantee —
``(signal_id, rung_index)`` and ``(signal_id, event_key)`` — so a test passing
here exercises the real mechanism rather than a mock of it, and they subclass the
real repositories so a drifting signature fails ``mypy --strict``.
``tests/tracker/test_persistence.py`` re-runs the scenarios against real Postgres.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sentinel.bot.models import (
    OPEN_STATUSES,
    SignalDecision,
    SignalRecord,
    SignalStatus,
    UserAccount,
    UserStatus,
)
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.ingestion.models import Candle, OHLCVSeries
from sentinel.risk.models import PauseState
from sentinel.storage.repositories import (
    RiskStateRepository,
    RuntimeSettingsRepository,
    SignalEventRepository,
    SignalExitRepository,
    SignalFillRepository,
    SignalRepository,
    UserMarketPauseRepository,
    UserRepository,
)
from sentinel.tracker.loop import TrackerRepositories
from tests.bot_double import OWNER_ID

_EPOCH = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


@dataclass
class FakeSignalRow:
    """The columns the tracker reads and writes."""

    id: UUID
    plan: dict[str, Any]
    symbol: str
    number: int = 1
    #: M8.1 — whose signal, and therefore whose loss limit and whose chat.
    user_id: int = OWNER_ID
    #: M10a — which market, and therefore which per-market loss rail it feeds.
    market: Market = LEGACY_MARKET
    #: M3 §7 / M10c join 2 — the ChartRenderParams record per attached chart.
    chart_params: list[Any] = field(default_factory=list)
    status: str = SignalStatus.PENDING_ENTRY.value
    decision: str | None = None
    dry_run: bool = False
    created_at: datetime | None = None
    expires_at: datetime | None = None
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    stop_price_current: Decimal | None = None
    tp_hits: int = 0
    realized_r: Decimal | None = None
    realized_eur: Decimal | None = None
    realized_costs_eur: Decimal | None = None
    outcome: str | None = None
    first_fill_at: datetime | None = None
    closed_at: datetime | None = None
    last_checked_at: datetime | None = None


@dataclass
class FakeStore:
    signals: dict[UUID, FakeSignalRow] = field(default_factory=dict)
    fills: dict[tuple[UUID, int], dict[str, Any]] = field(default_factory=dict)
    exits: dict[tuple[UUID, str], dict[str, Any]] = field(default_factory=dict)
    events: dict[tuple[UUID, str], dict[str, Any]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    pause: PauseState = field(default_factory=PauseState)
    #: The ``users`` rows the daily-loss rail divides each book by (M8.1).
    users: dict[int, UserAccount] = field(default_factory=dict)
    #: M10a — ``user_market_pauses``, the per-market half of that rail. ``pause``
    #: above is still the operator's global one.
    user_market_pauses: dict[tuple[int, Market], PauseState] = field(default_factory=dict)
    committed: int = 0

    def add_signal(self, record: SignalRecord, **overrides: Any) -> FakeSignalRow:
        row = FakeSignalRow(
            id=record.signal_id,
            plan=record.plan.model_dump(mode="json"),
            symbol=record.plan.symbol,
            number=record.number,
            user_id=record.user_id,
            market=record.market,
            status=record.status.value,
            decision=None if record.decision is None else record.decision.value,
            dry_run=record.dry_run,
            created_at=record.plan.created_at,
            expires_at=record.plan.expires_at,
            chart_params=list(record.chart_params),
        )
        for key, value in overrides.items():
            setattr(row, key, value)
        self.signals[row.id] = row
        return row

    def events_for(self, signal_id: UUID) -> list[dict[str, Any]]:
        return [row for (sid, _), row in self.events.items() if sid == signal_id]


class FakeSession:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def commit(self) -> None:
        self.store.committed += 1

    async def rollback(self) -> None:  # pragma: no cover — the tracker never rolls back
        pass


class FakeDatabase:
    """The one method of ``Database`` the tracker uses."""

    def __init__(self, store: FakeStore | None = None) -> None:
        self.store = store or FakeStore()
        self.sessions_opened = 0

    def session(self) -> Any:
        @asynccontextmanager
        async def _session() -> Any:
            self.sessions_opened += 1
            yield FakeSession(self.store)

        return _session()


def _store(session: Any) -> FakeStore:
    store: FakeStore = session.store
    return store


class FakeSignalRepository(SignalRepository):
    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store = _store(session)
        self._market = market

    async def open_signals(self) -> list[Any]:
        return [
            row for row in self._store.signals.values() if SignalStatus(row.status) in OPEN_STATUSES
        ]

    async def realized_eur_by_user_since(self, since: datetime) -> dict[int, list[Decimal]]:
        """This market's realized P&L, as the real one now filters it (M10a)."""
        return self._realized(since, market=self._market)

    async def realized_eur_by_user_since_across_markets(
        self, since: datetime
    ) -> dict[int, list[Decimal]]:
        """Every market's — the combined daily-loss rail's input."""
        return self._realized(since, market=None)

    def _realized(self, since: datetime, *, market: Market | None) -> dict[int, list[Decimal]]:
        grouped: dict[int, list[Decimal]] = {}
        for row in self._store.signals.values():
            if (
                row.decision == SignalDecision.TAKEN.value
                and not row.dry_run
                and row.closed_at is not None
                and row.closed_at >= since
                and row.realized_eur is not None
                and (market is None or row.market is market)
            ):
                grouped.setdefault(row.user_id, []).append(row.realized_eur)
        return grouped

    async def advance(self, signal_id: UUID, **fields: Any) -> Any:
        row = self._store.signals.get(signal_id)
        if row is None:  # pragma: no cover — the tick only advances rows it read
            return None
        for key, value in fields.items():
            if not hasattr(row, key):
                raise AttributeError(f"signals has no column {key!r}")
            setattr(row, key, value)
        return row


class FakeFillRepository(SignalFillRepository):
    def __init__(self, session: Any) -> None:
        self._store = _store(session)

    async def record(
        self,
        signal_id: UUID,
        *,
        rung_index: int,
        price: Decimal,
        qty: Decimal,
        filled_at: datetime,
        detected_at: datetime,
        source: str = "tracker",
    ) -> bool:
        key = (signal_id, rung_index)
        if key in self._store.fills:  # the real UNIQUE (signal_id, rung_index)
            return False
        self._store.fills[key] = {
            "signal_id": signal_id,
            "rung_index": rung_index,
            "price": price,
            "qty": qty,
            "filled_at": filled_at,
            "detected_at": detected_at,
            "source": source,
        }
        return True

    async def for_signal(self, signal_id: UUID) -> list[Any]:
        rows = [row for (sid, _), row in self._store.fills.items() if sid == signal_id]
        return [_Row(row) for row in sorted(rows, key=lambda row: row["rung_index"])]


class FakeExitRepository(SignalExitRepository):
    def __init__(self, session: Any) -> None:
        self._store = _store(session)

    async def record(
        self,
        signal_id: UUID,
        *,
        kind: str,
        price: Decimal,
        qty: Decimal,
        exited_at: datetime,
        detected_at: datetime,
    ) -> bool:
        key = (signal_id, kind)
        if key in self._store.exits:  # the real UNIQUE (signal_id, kind)
            return False
        self._store.exits[key] = {
            "signal_id": signal_id,
            "kind": kind,
            "price": price,
            "qty": qty,
            "exited_at": exited_at,
            "detected_at": detected_at,
        }
        return True

    async def for_signal(self, signal_id: UUID) -> list[Any]:
        rows = [row for (sid, _), row in self._store.exits.items() if sid == signal_id]
        return [_Row(row) for row in sorted(rows, key=lambda row: row["exited_at"])]


class FakeEventRepository(SignalEventRepository):
    def __init__(self, session: Any) -> None:
        self._store = _store(session)

    async def record(self, signal_id: UUID, *, event_key: str, **fields: Any) -> bool:
        key = (signal_id, event_key)
        if key in self._store.events:  # the real UNIQUE (signal_id, event_key)
            return False
        self._store.events[key] = {"signal_id": signal_id, "event_key": event_key, **fields}
        return True

    async def for_signal(self, signal_id: UUID) -> list[Any]:
        return [_Row(row) for row in self._store.events_for(signal_id)]


class FakeUserMarketPauseRepository(UserMarketPauseRepository):
    """``user_market_pauses``, in memory and keyed exactly as the real table is."""

    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store = _store(session)
        self._market = market

    async def load(self, user_id: int) -> PauseState:
        return self._store.user_market_pauses.get((user_id, self._market), PauseState())

    async def load_many(self, user_ids: Any) -> dict[int, PauseState]:
        return {user_id: await self.load(user_id) for user_id in user_ids}

    async def save(self, user_id: int, state: PauseState, *, at: datetime) -> None:
        self._store.user_market_pauses[(user_id, self._market)] = state


class FakeRiskStateRepository(RiskStateRepository):
    def __init__(self, session: Any) -> None:
        self._store = _store(session)

    async def load(self) -> PauseState:
        return self._store.pause

    async def save(self, state: PauseState) -> None:
        self._store.pause = state


class FakeSettingsRepository(RuntimeSettingsRepository):
    def __init__(self, session: Any) -> None:
        self._store = _store(session)

    async def all(self) -> dict[str, Any]:
        return dict(self._store.settings)


class FakeUserRepository(UserRepository):
    """The two calls the daily-loss rail makes (M8.1)."""

    def __init__(self, session: Any) -> None:
        self._store = _store(session)

    async def approved(self) -> list[UserAccount]:
        return sorted(
            (a for a in self._store.users.values() if a.status is UserStatus.APPROVED),
            key=lambda a: a.telegram_user_id,
        )

    async def set_pause(self, user_id: int, state: PauseState, *, at: datetime) -> None:
        existing = self._store.users.get(user_id)
        if existing is not None:
            self._store.users[user_id] = existing.model_copy(update={"pause": state})


class _Row:
    """Attribute access over a dict, so the fakes hand back row-shaped objects."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.__dict__.update(values)


def fake_repositories() -> TrackerRepositories:
    return TrackerRepositories(
        signals=FakeSignalRepository,
        fills=FakeFillRepository,
        exits=FakeExitRepository,
        events=FakeEventRepository,
        risk_state=FakeRiskStateRepository,
        settings=FakeSettingsRepository,
        users=FakeUserRepository,
        user_market_pause=FakeUserMarketPauseRepository,
    )


class ScriptedFeed:
    """Returns the candles a test hands it, and records what was asked for."""

    def __init__(
        self,
        candles: Sequence[Candle] = (),
        closed_1h: Sequence[Candle] = (),
        *,
        fail: Exception | None = None,
    ) -> None:
        self.candles = tuple(candles)
        self.closed_1h = tuple(closed_1h)
        self.fail = fail
        self.calls: list[tuple[str, str, int]] = []

    async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries:
        self.calls.append((symbol, timeframe, limit))
        if self.fail is not None:
            raise self.fail
        # ``invalidation_candles`` drops the last row as in-progress, so the 1h
        # reply carries one extra.
        candles = self.candles if timeframe == "1m" else (*self.closed_1h, _sentinel_candle())
        return OHLCVSeries(
            source="fake",
            fetched_at=_EPOCH,
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
        )


def _sentinel_candle() -> Candle:
    """The in-progress candle the feed must drop. Absurd values on purpose: a test
    that accidentally reads it fails loudly instead of passing quietly."""
    return Candle(
        open_time=_EPOCH,
        open=Decimal("9999"),
        high=Decimal("9999"),
        low=Decimal("9999"),
        close=Decimal("9999"),
        volume=Decimal("0"),
    )


__all__ = [
    "FakeDatabase",
    "FakeSession",
    "FakeSignalRow",
    "FakeStore",
    "ScriptedFeed",
    "fake_repositories",
]
