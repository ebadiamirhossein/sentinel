"""A fake Telegram bot, and an in-memory stand-in for the pieces of Postgres M6 uses.

Built in the mould of ``tests/ingestion/conftest.py``'s ``FakeExchange``: a
duck-typed object that records the calls made against it and can be told to fail a
specific one. Nothing here constructs an aiogram ``Bot`` or an HTTP session — the
autouse ``no_network`` guard in ``tests/conftest.py`` fails any test that tries to
dial out, which is how the suite proves it never touches live Telegram.

``FakeDatabase`` exists because the publisher's whole guarantee is an *ordering*
of commits around a send, and the message table's unique constraint is what makes
a restart safe. Asserting that against a mock of the repository would be asserting
our own mock; asserting it against a real Postgres would make the hermetic suite
need a database. So the two tables M6 owns are modelled here with their real
constraint behaviour, and ``tests/bot/test_persistence.py`` re-runs the same
scenarios against actual Postgres when one is available.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sentinel.bot.context import Repositories
from sentinel.bot.models import (
    OPEN_STATUSES,
    MessageKind,
    MessageStatus,
    PostedMessage,
    SignalDecision,
    SignalStatus,
)
from sentinel.ingestion.models import InstrumentMeta
from sentinel.llm.spend import SpendTotals
from sentinel.risk.models import PauseState
from sentinel.storage.repositories import (
    CycleRepository,
    InstrumentMetaRepository,
    LLMCallRepository,
    RiskStateRepository,
    RuntimeSettingsRepository,
    SignalEventRepository,
    SignalExitRepository,
    SignalFillRepository,
    SignalRepository,
    SnapshotRepository,
    TelegramMessageRepository,
)


@dataclass
class SentMessage:
    """One outbound Telegram call, as the bot made it."""

    method: str
    kwargs: dict[str, Any]
    message_id: int


class _Message:
    """The subset of ``aiogram.types.Message`` the publisher reads back."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    """Records what would have been sent; raises what it is told to raise."""

    def __init__(self, *, fail: dict[str, Exception] | None = None) -> None:
        self.calls: list[SentMessage] = []
        self._fail = fail or {}
        self._next_message_id = 1000

    # -- outbound API ------------------------------------------------------ #

    async def send_message(self, **kwargs: Any) -> _Message:
        return self._record("send_message", kwargs)

    async def send_media_group(self, **kwargs: Any) -> list[_Message]:
        return [self._record("send_media_group", kwargs)]

    async def edit_message_reply_markup(self, **kwargs: Any) -> _Message:
        return self._record("edit_message_reply_markup", kwargs)

    async def answer_callback_query(self, **kwargs: Any) -> bool:
        self._record("answer_callback_query", kwargs)
        return True

    # -- assertion surface ------------------------------------------------- #

    @property
    def texts(self) -> list[str]:
        return [call.kwargs["text"] for call in self.calls if "text" in call.kwargs]

    def of(self, method: str) -> list[SentMessage]:
        return [call for call in self.calls if call.method == method]

    def _record(self, method: str, kwargs: dict[str, Any]) -> _Message:
        self.calls.append(SentMessage(method, kwargs, self._next_message_id))
        if method in self._fail:
            raise self._fail[method]
        message = _Message(self._next_message_id)
        self._next_message_id += 1
        return message


# --------------------------------------------------------------------------- #
# In-memory storage for the two tables the publisher's guarantee rests on
# --------------------------------------------------------------------------- #


@dataclass
class _SignalRow:
    signal_id: UUID
    plan_id: UUID
    number: int
    decision: str | None = None
    decided_at: datetime | None = None
    decided_by_user_id: int | None = None
    # M7: what the tracker writes, and what /positions and /status read back.
    symbol: str = "SOLUSDT"
    direction: str = "long"
    setup_type: str = "trend_pullback"
    plan: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None
    status: str = "PENDING_ENTRY"
    dry_run: bool = False
    created_at: datetime | None = None
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    stop_price_current: Decimal | None = None
    tp_hits: int = 0
    realized_r: Decimal | None = None
    realized_eur: Decimal | None = None
    outcome: str | None = None
    closed_at: datetime | None = None

    @property
    def id(self) -> UUID:
        """``signals.id`` — the fake names it ``signal_id``, the column is ``id``."""
        return self.signal_id


@dataclass
class _MessageRow:
    signal_id: UUID
    kind: str
    chat_id: int
    #: M7 widened the unique key to include this — a signal's thread carries many
    #: updates, and ``kind`` alone allowed exactly one.
    event_key: str = ""
    status: str = "PENDING"
    message_id: int | None = None
    error: str | None = None


@dataclass
class _SnapshotRow:
    symbol: str
    data_quality: str
    captured_at: datetime
    degraded_fields: list[str] = field(default_factory=list)


@dataclass
class FakeStore:
    """Shared state that survives a "restart" — exactly what a database is for."""

    signals: dict[UUID, _SignalRow] = field(default_factory=dict)
    messages: dict[tuple[UUID, str, int, str], _MessageRow] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    changes: list[tuple[str, Any, Any, int | None]] = field(default_factory=list)
    snapshots: list[_SnapshotRow] = field(default_factory=list)
    instruments: dict[str, InstrumentMeta] = field(default_factory=dict)
    pause: PauseState = field(default_factory=PauseState)
    # M7: the tracker's tables, keyed exactly as their unique constraints are.
    fills: dict[tuple[UUID, int], dict[str, Any]] = field(default_factory=dict)
    exits: dict[tuple[UUID, str], dict[str, Any]] = field(default_factory=dict)
    events: dict[tuple[UUID, str], dict[str, Any]] = field(default_factory=dict)
    last_cycle: Any = None
    #: Newest first, as ``CycleRepository.recent`` returns them (M8's alerts).
    cycles: list[Any] = field(default_factory=list)
    cycles_completed: int = 0
    cycles_started: int = 0
    spend: SpendTotals = field(default_factory=SpendTotals)
    committed: int = 0
    rolled_back: int = 0

    def by_plan_id(self, plan_id: UUID) -> _SignalRow | None:
        return next((row for row in self.signals.values() if row.plan_id == plan_id), None)


class FakeSession:
    """Only the two calls the publisher makes on a session."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def commit(self) -> None:
        self.store.committed += 1

    async def rollback(self) -> None:
        self.store.rolled_back += 1


class FakeDatabase:
    """``Database``-shaped, with the unique constraints that matter modelled."""

    def __init__(self, store: FakeStore | None = None) -> None:
        self.store = store or FakeStore()
        self.sessions_opened = 0

    @asynccontextmanager
    async def session(self) -> AsyncIterator[FakeSession]:
        self.sessions_opened += 1
        yield FakeSession(self.store)


class FakeSignalStore:
    """``SignalRepository.claim`` with the real ``plan_id`` unique constraint."""

    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def claim(self, record: Any) -> Any:
        if self._store.by_plan_id(record.plan.plan_id) is not None:
            return None
        number = len(self._store.signals) + 1
        self._store.signals[record.signal_id] = _SignalRow(
            signal_id=record.signal_id, plan_id=record.plan.plan_id, number=number
        )
        return record.model_copy(update={"number": number})


class FakeMessageStore:
    """The real ``(signal_id, kind, chat_id, event_key)`` constraint, in memory.

    ``event_key`` joined the key at M7 and defaults to ``""`` for the album and
    the card — which is exactly how the column is defined, ``NOT NULL DEFAULT ''``.
    A nullable one would have let Postgres treat two NULLs as distinct and quietly
    un-guaranteed the card's own idempotency, so the fake models the same thing.
    """

    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def claim(
        self,
        signal_id: UUID,
        kind: Any,
        chat_id: int,
        *,
        at: datetime,
        event_key: str = "",
    ) -> bool:
        key = (signal_id, kind.value, chat_id, event_key)
        if key in self._store.messages:
            return False
        self._store.messages[key] = _MessageRow(
            signal_id=signal_id, kind=kind.value, chat_id=chat_id, event_key=event_key
        )
        return True

    async def confirm(
        self,
        signal_id: UUID,
        kind: Any,
        chat_id: int,
        *,
        message_id: int,
        at: datetime,
        event_key: str = "",
    ) -> None:
        row = self._store.messages[(signal_id, kind.value, chat_id, event_key)]
        row.message_id = message_id
        row.status = "SENT"

    async def fail(
        self,
        signal_id: UUID,
        kind: Any,
        chat_id: int,
        *,
        error: str,
        event_key: str = "",
    ) -> None:
        row = self._store.messages[(signal_id, kind.value, chat_id, event_key)]
        row.status = "FAILED"
        row.error = error

    async def get(
        self, signal_id: UUID, kind: Any, chat_id: int, event_key: str = ""
    ) -> PostedMessage | None:
        row = self._store.messages.get((signal_id, kind.value, chat_id, event_key))
        if row is None:
            return None
        return PostedMessage(
            signal_id=row.signal_id,
            kind=MessageKind(row.kind),
            chat_id=row.chat_id,
            event_key=row.event_key,
            message_id=row.message_id,
            status=MessageStatus(row.status),
            error=row.error,
        )


# --------------------------------------------------------------------------- #
# Repository fakes for the command handlers
#
# Each subclasses the real repository, so a signature that drifts fails
# ``mypy --strict`` here rather than diverging silently. They read and write the
# same ``FakeStore`` the publisher fakes use, so a test can press a button and
# then run ``/positions`` against the result.
# --------------------------------------------------------------------------- #


class FakeSettingsRepository(RuntimeSettingsRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def all(self) -> dict[str, Any]:
        return dict(self._store.settings)

    async def get(self, key: str) -> Any | None:
        return self._store.settings.get(key)

    async def set(self, key: str, value: Any, *, at: datetime, user_id: int | None) -> None:
        self._store.changes.append((key, self._store.settings.get(key), value, user_id))
        self._store.settings[key] = value

    async def changes(self, limit: int = 50) -> list[Any]:
        return list(self._store.changes[:limit])


class FakeRiskStateRepository(RiskStateRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def load(self) -> PauseState:
        return self._store.pause

    async def save(self, state: PauseState) -> None:
        self._store.pause = state


class FakeSignalRepository(SignalRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def claim(self, record: Any) -> Any:
        return await FakeSignalStore(session_of(self._store)).claim(record)

    async def record_decision(
        self, signal_id: UUID, decision: Any, *, at: datetime, user_id: int
    ) -> Any:
        row = self._store.signals.get(signal_id)
        if row is None:
            return None
        if row.decision == decision.value:
            return row, False
        row.decision = decision.value
        row.decided_at = at
        row.decided_by_user_id = user_id
        return row, True

    async def with_decision(self, decision: Any, *, limit: int = 50) -> list[Any]:
        return [row for row in self._store.signals.values() if row.decision == decision.value]

    async def recent(self, limit: int = 20) -> list[Any]:
        return list(self._store.signals.values())[:limit]

    async def undecided_count(self) -> int:
        return sum(1 for row in self._store.signals.values() if row.decision is None)

    async def open_taken(self) -> list[Any]:
        return [
            row
            for row in self._store.signals.values()
            if SignalStatus(row.status) in OPEN_STATUSES
            and row.decision == SignalDecision.TAKEN.value
            and not row.dry_run
        ]

    async def open_symbols(self) -> set[str]:
        return {
            getattr(row, "symbol", "")
            for row in self._store.signals.values()
            if SignalStatus(row.status) in OPEN_STATUSES
        }

    async def get(self, signal_id: UUID) -> Any:
        return self._store.signals.get(signal_id)

    async def advance(self, signal_id: UUID, **fields: Any) -> Any:
        row = self._store.signals.get(signal_id)
        if row is None:  # pragma: no cover
            return None
        for key, value in fields.items():
            if not hasattr(row, key):
                raise AttributeError(f"signals has no column {key!r}")
            setattr(row, key, value)
        return row

    async def published_since(self, since: datetime) -> int:
        return sum(
            1
            for row in self._store.signals.values()
            if row.created_at is not None and row.created_at >= since
        )


class _Row:
    """Attribute access over a dict, so a fake hands back row-shaped objects."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.__dict__.update(values)


class FakeFillRepository(SignalFillRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

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
        if key in self._store.fills:
            return False
        self._store.fills[key] = {"price": price, "qty": qty, "rung_index": rung_index}
        return True

    async def for_signal(self, signal_id: UUID) -> list[Any]:
        return [_Row(values) for (sid, _), values in self._store.fills.items() if sid == signal_id]

    async def for_signals(self, signal_ids: Any) -> dict[UUID, list[Any]]:
        wanted = set(signal_ids)
        grouped: dict[UUID, list[Any]] = {}
        for (sid, _), values in self._store.fills.items():
            if sid in wanted:
                grouped.setdefault(sid, []).append(_Row(values))
        return grouped


class FakeExitRepository(SignalExitRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

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
        if key in self._store.exits:
            return False
        self._store.exits[key] = {"kind": kind, "price": price, "qty": qty}
        return True

    async def for_signal(self, signal_id: UUID) -> list[Any]:
        return [_Row(values) for (sid, _), values in self._store.exits.items() if sid == signal_id]

    async def for_signals(self, signal_ids: Any) -> dict[UUID, list[Any]]:
        wanted = set(signal_ids)
        grouped: dict[UUID, list[Any]] = {}
        for (sid, _), values in self._store.exits.items():
            if sid in wanted:
                grouped.setdefault(sid, []).append(_Row(values))
        return grouped


class FakeEventRepository(SignalEventRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def record(self, signal_id: UUID, *, event_key: str, **fields: Any) -> bool:
        key = (signal_id, event_key)
        if key in self._store.events:
            return False
        self._store.events[key] = {"signal_id": signal_id, "event_key": event_key, **fields}
        return True

    async def unposted(self, chat_id: int, *, limit: int = 100) -> list[Any]:
        posted = {
            key[3] for key in self._store.messages if key[1] == "update" and key[2] == chat_id
        }
        return [
            _Row(values)
            for (_, event_key), values in self._store.events.items()
            if event_key not in posted
        ]


class FakeCycleRepository(CycleRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def latest(self) -> Any:
        return self._store.last_cycle

    async def recent(self, limit: int = 10) -> list[Any]:
        return list(self._store.cycles[:limit])

    async def completion_since(self, since: datetime) -> tuple[int, int]:
        return self._store.cycles_completed, self._store.cycles_started


class FakeLLMCallRepository(LLMCallRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def spend_totals(
        self, *, day_start: datetime, month_start: datetime, priced_models: Any
    ) -> SpendTotals:
        return self._store.spend


class FakeSnapshotRepository(SnapshotRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def latest_per_symbol(self, limit: int = 20) -> list[Any]:
        return list(self._store.snapshots[:limit])


class FakeMessageRepository(TelegramMessageRepository):
    """Enforces the real ``(signal_id, kind, chat_id, event_key)`` unique key."""

    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def stuck(self) -> list[Any]:
        return [row for row in self._store.messages.values() if row.status != "SENT"]

    async def claim(
        self,
        signal_id: UUID,
        kind: Any,
        chat_id: int,
        *,
        at: datetime,
        event_key: str = "",
    ) -> bool:
        key = (signal_id, kind.value, chat_id, event_key)
        if key in self._store.messages:
            return False
        self._store.messages[key] = _MessageRow(
            signal_id=signal_id, kind=kind.value, chat_id=chat_id, event_key=event_key
        )
        return True

    async def confirm(
        self,
        signal_id: UUID,
        kind: Any,
        chat_id: int,
        *,
        message_id: int,
        at: datetime,
        event_key: str = "",
    ) -> None:
        row = self._store.messages.get((signal_id, kind.value, chat_id, event_key))
        if row is None:  # pragma: no cover — confirm follows a successful claim
            return
        row.message_id = message_id
        row.status = "SENT"

    async def fail(
        self,
        signal_id: UUID,
        kind: Any,
        chat_id: int,
        *,
        error: str,
        event_key: str = "",
    ) -> None:
        row = self._store.messages.get((signal_id, kind.value, chat_id, event_key))
        if row is None:  # pragma: no cover
            return
        row.status = "FAILED"
        row.error = error

    async def get(
        self, signal_id: UUID, kind: Any, chat_id: int, event_key: str = ""
    ) -> PostedMessage | None:
        row = self._store.messages.get((signal_id, kind.value, chat_id, event_key))
        if row is None:
            return None
        return PostedMessage(
            signal_id=row.signal_id,
            kind=MessageKind(row.kind),
            chat_id=row.chat_id,
            event_key=row.event_key,
            message_id=row.message_id,
            status=MessageStatus(row.status),
            error=row.error,
        )

    async def claimed_message(self, chat_id: int, message_id: int) -> tuple[UUID, str] | None:
        for row in self._store.messages.values():
            if row.chat_id == chat_id and row.message_id == message_id:
                return row.signal_id, row.event_key
        return None


class FakeInstrumentRepository(InstrumentMetaRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def get(self, symbol: str) -> InstrumentMeta | None:
        return self._store.instruments.get(symbol)


def session_of(store: FakeStore) -> FakeSession:
    return FakeSession(store)


def fake_repositories() -> Repositories:
    return Repositories(
        signals=FakeSignalRepository,
        messages=FakeMessageRepository,
        settings=FakeSettingsRepository,
        risk_state=FakeRiskStateRepository,
        snapshots=FakeSnapshotRepository,
        instruments=FakeInstrumentRepository,
        fills=FakeFillRepository,
        exits=FakeExitRepository,
        events=FakeEventRepository,
        cycles=FakeCycleRepository,
        llm_calls=FakeLLMCallRepository,
    )


__all__ = [
    "FakeBot",
    "FakeDatabase",
    "FakeMessageStore",
    "FakeSession",
    "FakeSignalStore",
    "FakeStore",
    "SentMessage",
    "fake_repositories",
]
