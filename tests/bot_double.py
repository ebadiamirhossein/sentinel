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
from typing import Any
from uuid import UUID

from sentinel.bot.context import Repositories
from sentinel.ingestion.models import InstrumentMeta
from sentinel.risk.models import PauseState
from sentinel.storage.repositories import (
    InstrumentMetaRepository,
    RiskStateRepository,
    RuntimeSettingsRepository,
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


@dataclass
class _MessageRow:
    signal_id: UUID
    kind: str
    chat_id: int
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
    messages: dict[tuple[UUID, str, int], _MessageRow] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    changes: list[tuple[str, Any, Any, int | None]] = field(default_factory=list)
    snapshots: list[_SnapshotRow] = field(default_factory=list)
    instruments: dict[str, InstrumentMeta] = field(default_factory=dict)
    pause: PauseState = field(default_factory=PauseState)
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
    """``TelegramMessageRepository`` with the ``(signal, kind, chat)`` constraint."""

    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def claim(self, signal_id: UUID, kind: Any, chat_id: int, *, at: datetime) -> bool:
        key = (signal_id, kind.value, chat_id)
        if key in self._store.messages:
            return False
        self._store.messages[key] = _MessageRow(
            signal_id=signal_id, kind=kind.value, chat_id=chat_id
        )
        return True

    async def confirm(
        self, signal_id: UUID, kind: Any, chat_id: int, *, message_id: int, at: datetime
    ) -> None:
        row = self._store.messages[(signal_id, kind.value, chat_id)]
        row.message_id = message_id
        row.status = "SENT"

    async def fail(self, signal_id: UUID, kind: Any, chat_id: int, *, error: str) -> None:
        row = self._store.messages[(signal_id, kind.value, chat_id)]
        row.status = "FAILED"
        row.error = error


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


class FakeSnapshotRepository(SnapshotRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def latest_per_symbol(self, limit: int = 20) -> list[Any]:
        return list(self._store.snapshots[:limit])


class FakeMessageRepository(TelegramMessageRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def stuck(self) -> list[Any]:
        return [row for row in self._store.messages.values() if row.status != "SENT"]


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
