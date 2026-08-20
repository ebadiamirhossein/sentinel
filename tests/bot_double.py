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
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from typing import Any
from uuid import UUID

from sentinel.bot.context import Repositories
from sentinel.bot.models import (
    ACK_VERSION,
    OPEN_STATUSES,
    MessageKind,
    MessageStatus,
    PostedMessage,
    SignalDecision,
    SignalStatus,
    UserAccount,
    UserRole,
    UserStatus,
    WatchlistRequest,
)
from sentinel.ingestion.models import InstrumentMeta
from sentinel.llm.spend import SpendTotals
from sentinel.risk.models import PauseState
from sentinel.screener.models import ScreenerVerdict
from sentinel.storage.repositories import (
    AnalystReportRepository,
    CycleRepository,
    GateDecisionRepository,
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
    UserRepository,
    WatchlistRequestRepository,
)

#: Every fake account's ``requested_at``/``decided_at``. Frozen, like the clock.
ACCOUNT_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


#: The default owner in the M6/M7 suites, kept as the default ``user_id`` on a fake
#: signal row so tests written before M8.1 keep asserting the same thing.
OWNER_ID = 111


def owner_account(
    user_id: int = OWNER_ID,
    *,
    capital_eur: Decimal | None = None,
    risk_per_trade_pct: Decimal | None = None,
    acknowledged: bool = True,
    status: UserStatus = UserStatus.APPROVED,
    role: UserRole = UserRole.OWNER,
    username: str | None = None,
    pause: PauseState | None = None,
) -> UserAccount:
    """A ``users`` row for a test, approved and acknowledged unless told otherwise."""
    return UserAccount(
        telegram_user_id=user_id,
        status=status,
        role=role,
        username=username,
        requested_at=ACCOUNT_NOW,
        decided_at=ACCOUNT_NOW,
        capital_eur=capital_eur,
        risk_per_trade_pct=risk_per_trade_pct,
        acknowledged_at=ACCOUNT_NOW if acknowledged else None,
        acknowledged_version=ACK_VERSION if acknowledged else "",
        pause=pause or PauseState(),
    )


def member_account(user_id: int, **kwargs: Any) -> UserAccount:
    """A member row — the same thing without the OWNER role."""
    kwargs.setdefault("role", UserRole.MEMBER)
    return owner_account(user_id, **kwargs)


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

    # -- the command menu (M8.1) ------------------------------------------- #

    async def set_my_commands(self, **kwargs: Any) -> bool:
        self._record("set_my_commands", kwargs)
        return True

    async def delete_my_commands(self, **kwargs: Any) -> bool:
        self._record("delete_my_commands", kwargs)
        return True

    def scopes(self, method: str = "set_my_commands") -> dict[str, list[str]]:
        """``{scope description: command names}`` — what the "/" menu would show.

        A ``BotCommandScopeChat`` is keyed by its chat id and everything else by its
        type name, which is exactly the distinction the M8.1 menu makes: the default
        scope is what a stranger sees and a per-chat scope is what standing earns.
        """
        out: dict[str, list[str]] = {}
        for call in self.of(method):
            scope = call.kwargs["scope"]
            key = str(getattr(scope, "chat_id", type(scope).__name__))
            out[key] = [command.command for command in call.kwargs.get("commands", [])]
        return out

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
    #: M8.1 — whose signal this is. Every read model filters on it.
    user_id: int = OWNER_ID
    decision: str | None = None
    decided_at: datetime | None = None
    decided_by_user_id: int | None = None
    # M7: what the tracker writes, and what /positions and /status read back.
    symbol: str = "SOLUSDT"
    direction: str = "long"
    setup_type: str = "trend_pullback"
    #: /stats breaks its populations down by this (specs/PROMPTS.md §5 step 3).
    prompt_version: str | None = "fable_v1"
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
    realized_costs_eur: Decimal | None = None
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
    #: M8.1's ``users`` table, keyed by its primary key so ``request``'s
    #: ON CONFLICT DO NOTHING behaves exactly as Postgres does.
    users: dict[int, UserAccount] = field(default_factory=dict)
    messages: dict[tuple[UUID, str, int, str], _MessageRow] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    #: M8.3 — ``watchlist_requests``, keyed by symbol for the rows that are
    #: PENDING, exactly as the partial unique index constrains them. Decided
    #: rows move to ``watchlist_history`` so a symbol can be asked for again.
    watchlist_requests: dict[str, Any] = field(default_factory=dict)
    watchlist_history: list[Any] = field(default_factory=list)
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
    # M8.4: what /pulse reads. These are the pipeline's own record, written only by
    # the orchestrator — no handler in the bot writes any of them, which is why the
    # fakes below expose reads and nothing else.
    completed_cycles: list[Any] = field(default_factory=list)
    screener: dict[Any, tuple[ScreenerVerdict, ...]] = field(default_factory=dict)
    reports: list[Any] = field(default_factory=list)
    gate_decisions: list[Any] = field(default_factory=list)
    cycles_completed: int = 0
    cycles_started: int = 0
    spend: SpendTotals = field(default_factory=SpendTotals)
    committed: int = 0
    rolled_back: int = 0

    def by_plan_id(self, plan_id: UUID) -> _SignalRow | None:
        return next((row for row in self.signals.values() if row.plan_id == plan_id), None)


class _EmptyRows:
    """What a SELECT returns from this fake: no rows.

    Most repositories here are replaced by fakes, but a few real ones — the stats
    queries behind ``/stats``, for instance — are used unmocked by the dispatcher
    wiring tests, which care about *routing* rather than about results. Answering
    reads honestly ("this database is empty") lets those run without every test
    having to wire a double it does not care about.
    """

    def __iter__(self) -> Any:
        return iter(())

    def all(self) -> list[Any]:
        return []

    def scalars(self) -> _EmptyRows:
        return self

    def first(self) -> None:
        return None

    def one_or_none(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None


class FakeSession:
    """Only the calls the publisher and the read models make on a session."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def execute(self, *args: Any, **kwargs: Any) -> _EmptyRows:
        return _EmptyRows()

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


class FakeUserRepository(UserRepository):
    """The ``users`` table in memory, with its primary key doing the real work."""

    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def get(self, user_id: int) -> UserAccount | None:
        return self._store.users.get(user_id)

    async def owner(self) -> UserAccount | None:
        return next((a for a in self._store.users.values() if a.is_owner), None)

    async def all(self) -> list[UserAccount]:
        return sorted(
            self._store.users.values(), key=lambda a: (a.requested_at, a.telegram_user_id)
        )

    async def approved(self) -> list[UserAccount]:
        return sorted(
            (a for a in self._store.users.values() if a.status is UserStatus.APPROVED),
            key=lambda a: a.telegram_user_id,
        )

    async def request(
        self, user_id: int, *, username: str | None, display_name: str | None, at: datetime
    ) -> UserAccount | None:
        if user_id in self._store.users:
            return None  # ON CONFLICT DO NOTHING — a re-request writes nothing
        created = UserAccount(
            telegram_user_id=user_id,
            status=UserStatus.PENDING,
            role=UserRole.MEMBER,
            username=username,
            display_name=display_name,
            requested_at=at,
        )
        self._store.users[user_id] = created
        return created

    async def ensure_owner(
        self, user_id: int, *, at: datetime, acknowledged_version: str
    ) -> UserAccount:
        existing = self._store.users.get(user_id)
        if existing is not None:
            return existing
        seeded = UserAccount(
            telegram_user_id=user_id,
            status=UserStatus.APPROVED,
            role=UserRole.OWNER,
            requested_at=at,
            decided_at=at,
            decided_by_user_id=user_id,
            acknowledged_at=at,
            acknowledged_version=acknowledged_version,
        )
        self._store.users[user_id] = seeded
        return seeded

    async def set_status(
        self, user_id: int, status: UserStatus, *, at: datetime, by_user_id: int | None
    ) -> UserAccount | None:
        return self._update(user_id, status=status, decided_at=at, decided_by_user_id=by_user_id)

    async def acknowledge(self, user_id: int, *, version: str, at: datetime) -> UserAccount | None:
        return self._update(user_id, acknowledged_at=at, acknowledged_version=version)

    async def set_capital(
        self, user_id: int, capital_eur: Decimal, *, at: datetime
    ) -> UserAccount | None:
        return self._audited(user_id, "capital_eur", capital_eur)

    async def set_risk_pct(
        self, user_id: int, risk_per_trade_pct: Decimal, *, at: datetime
    ) -> UserAccount | None:
        return self._audited(user_id, "risk_per_trade_pct", risk_per_trade_pct)

    def _audited(self, user_id: int, column: str, value: Decimal) -> UserAccount | None:
        """Mirrors the real repository: the sizing inputs append a config_changes row."""
        previous = self._store.users.get(user_id)
        if previous is None:
            return None
        old = getattr(previous, column)
        self._store.changes.append(
            (f"user.{user_id}.{column}", None if old is None else str(old), str(value), user_id)
        )
        return self._update(user_id, **{column: value})

    async def set_pause(self, user_id: int, state: PauseState, *, at: datetime) -> None:
        self._update(user_id, pause=state)

    async def touch_notice(self, user_id: int, *, at: datetime) -> None:
        self._update(user_id, notice_at=at)

    def _update(self, user_id: int, **fields: Any) -> UserAccount | None:
        existing = self._store.users.get(user_id)
        if existing is None:
            return None
        updated = existing.model_copy(update=fields)
        self._store.users[user_id] = updated
        return updated


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

    def _mine(self, user_id: int) -> list[_SignalRow]:
        return [row for row in self._store.signals.values() if row.user_id == user_id]

    async def with_decision(self, decision: Any, *, user_id: int, limit: int = 50) -> list[Any]:
        return [row for row in self._mine(user_id) if row.decision == decision.value][:limit]

    async def recent(self, *, user_id: int, limit: int = 20) -> list[Any]:
        return self._mine(user_id)[:limit]

    async def undecided_count(self, *, user_id: int) -> int:
        return sum(1 for row in self._mine(user_id) if row.decision is None)

    async def open_taken(self, *, user_id: int) -> list[Any]:
        return [
            row
            for row in self._mine(user_id)
            if SignalStatus(row.status) in OPEN_STATUSES
            and row.decision == SignalDecision.TAKEN.value
            and not row.dry_run
        ]

    async def open_symbols(self, *, user_id: int) -> set[str]:
        return {
            row.symbol for row in self._mine(user_id) if SignalStatus(row.status) in OPEN_STATUSES
        }

    async def open_symbols_by_user(self) -> dict[int, set[str]]:
        grouped: dict[int, set[str]] = {}
        for row in self._store.signals.values():
            if SignalStatus(row.status) in OPEN_STATUSES:
                grouped.setdefault(row.user_id, set()).add(row.symbol)
        return grouped

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

    async def published_since(self, since: datetime, *, user_id: int) -> int:
        return sum(
            1
            for row in self._mine(user_id)
            if row.created_at is not None and row.created_at >= since
        )

    async def published_by_user_since(self, since: datetime) -> dict[int, int]:
        grouped: dict[int, int] = {}
        for row in self._store.signals.values():
            if row.created_at is not None and row.created_at >= since:
                grouped[row.user_id] = grouped.get(row.user_id, 0) + 1
        return grouped

    async def resolutions_since(
        self, since: datetime, *, user_id: int
    ) -> list[tuple[str, datetime]]:
        return [
            (row.symbol, row.closed_at)
            for row in self._mine(user_id)
            if row.closed_at is not None
            and row.closed_at >= since
            and row.status in _ARMING_STATUSES
        ]

    async def resolutions_by_user_since(
        self, since: datetime
    ) -> dict[int, list[tuple[str, datetime]]]:
        grouped: dict[int, list[tuple[str, datetime]]] = {}
        for row in self._store.signals.values():
            if (
                row.closed_at is not None
                and row.closed_at >= since
                and row.status in _ARMING_STATUSES
            ):
                grouped.setdefault(row.user_id, []).append((row.symbol, row.closed_at))
        return grouped

    async def realized_eur_by_user_since(self, since: datetime) -> dict[int, list[Decimal]]:
        grouped: dict[int, list[Decimal]] = {}
        for row in self._store.signals.values():
            if (
                row.decision == SignalDecision.TAKEN.value
                and not row.dry_run
                and row.closed_at is not None
                and row.closed_at >= since
                and row.realized_eur is not None
            ):
                grouped.setdefault(row.user_id, []).append(row.realized_eur)
        return grouped

    async def resolved_since(self, since: datetime | None = None, *, user_id: int) -> list[Any]:
        return [
            row
            for row in self._mine(user_id)
            if row.closed_at is not None and (since is None or row.closed_at >= since)
        ]


#: Which statuses arm a cooldown, mirroring ``repositories._COOLDOWN_ARMING``.
_ARMING_STATUSES = (
    SignalStatus.STOPPED.value,
    SignalStatus.EXPIRED.value,
    SignalStatus.INVALIDATED.value,
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

    async def unposted(self, chat_id: int, *, user_id: int, limit: int = 100) -> list[Any]:
        posted = {
            key[3] for key in self._store.messages if key[1] == "update" and key[2] == chat_id
        }
        mine = {
            signal_id for signal_id, row in self._store.signals.items() if row.user_id == user_id
        }
        return [
            _Row(values)
            for (signal_id, event_key), values in self._store.events.items()
            if event_key not in posted and signal_id in mine
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

    async def latest_completed(self) -> Any:
        """Newest finished cycle. A separate list from ``cycles``, on purpose.

        The real query filters on ``finished_at IS NOT NULL`` and orders by it, so a
        fake that returned ``last_cycle`` would let a test pass while ``/pulse``
        narrated a cycle still in its screener — the exact case
        :meth:`CycleRepository.latest_completed` exists to exclude.
        """
        return self._store.completed_cycles[-1] if self._store.completed_cycles else None

    async def completed_since(self, since: datetime, limit: int = 200) -> list[Any]:
        return [cycle for cycle in self._store.completed_cycles if cycle.started_at >= since][
            :limit
        ]


class FakeLLMCallRepository(LLMCallRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def spend_totals(
        self, *, day_start: datetime, month_start: datetime, priced_models: Any
    ) -> SpendTotals:
        return self._store.spend

    async def screener_verdicts(self, cycle_ids: Any) -> dict[Any, tuple[ScreenerVerdict, ...]]:
        """Verdicts by cycle.

        The real one digs them out of ``llm_calls.response -> parsed -> verdicts``
        and validates each entry; this returns them already made. The JSONB read
        itself is covered where only a real database can cover it —
        ``tests/bot/test_persistence.py`` — and the pure parser in
        ``storage.screener_verdicts_of`` is tested directly.
        """
        wanted = set(cycle_ids)
        return {
            cycle_id: verdicts
            for cycle_id, verdicts in self._store.screener.items()
            if cycle_id in wanted
        }


class FakeAnalystReportRepository(AnalystReportRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def for_cycles(self, cycle_ids: Any, *, role: str = "primary") -> list[Any]:
        wanted = set(cycle_ids)
        return [row for row in self._store.reports if row.cycle_id in wanted]

    async def latest_for_symbol(self, symbol: str, *, role: str = "primary") -> Any:
        """Newest first, as the real ``ORDER BY created_at DESC`` returns them.

        ``store.reports`` is seeded oldest-first, so this reverses rather than sorts —
        the ordering itself is what ``tests/bot/test_persistence.py`` proves against a
        real database, since only Postgres runs the actual ``ORDER BY``.
        """
        for row in reversed(self._store.reports):
            if row.symbol == symbol and row.role == role:
                return row
        return None


class FakeGateDecisionRepository(GateDecisionRepository):
    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def for_cycles(self, cycle_ids: Any) -> list[Any]:
        wanted = set(cycle_ids)
        return [row for row in self._store.gate_decisions if row.cycle_id in wanted]


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


class FakeWatchlistRequestRepository(WatchlistRequestRepository):
    """M8.3's requests in memory, with the partial unique index modelled.

    ``request`` returns ``None`` when a PENDING row already exists for the symbol —
    which is the whole contract, and is enforced in Postgres by
    ``uq_watchlist_requests_one_pending`` rather than by this dict. The real index is
    asserted against a real database in ``tests/llm/test_persistence.py``.
    """

    _next_id = count(9000)

    def __init__(self, session: Any) -> None:
        self._store: FakeStore = session.store

    async def request(self, symbol: str, *, user_id: int, at: datetime) -> Any:
        if symbol in self._store.watchlist_requests:
            return None
        row = WatchlistRequest(
            id=next(FakeWatchlistRequestRepository._next_id),
            symbol=symbol,
            requested_by_user_id=user_id,
            requested_at=at,
        )
        self._store.watchlist_requests[symbol] = row
        return row

    async def pending_for(self, symbol: str) -> Any:
        return self._store.watchlist_requests.get(symbol)

    async def pending(self) -> list[Any]:
        return sorted(self._store.watchlist_requests.values(), key=lambda r: r.requested_at)

    async def decide(self, symbol: str, status: Any, *, by: int, at: datetime) -> Any:
        row = self._store.watchlist_requests.pop(symbol, None)
        if row is None:
            return None
        decided = row.model_copy(
            update={"status": status, "decided_at": at, "decided_by_user_id": by}
        )
        self._store.watchlist_history.append(decided)
        return decided


def fake_repositories() -> Repositories:
    return Repositories(
        signals=FakeSignalRepository,
        messages=FakeMessageRepository,
        settings=FakeSettingsRepository,
        users=FakeUserRepository,
        risk_state=FakeRiskStateRepository,
        snapshots=FakeSnapshotRepository,
        instruments=FakeInstrumentRepository,
        fills=FakeFillRepository,
        exits=FakeExitRepository,
        events=FakeEventRepository,
        cycles=FakeCycleRepository,
        llm_calls=FakeLLMCallRepository,
        watchlist_requests=FakeWatchlistRequestRepository,
        reports=FakeAnalystReportRepository,
        gate_decisions=FakeGateDecisionRepository,
    )


__all__ = [
    "ACCOUNT_NOW",
    "OWNER_ID",
    "FakeBot",
    "FakeDatabase",
    "FakeMessageStore",
    "FakeSession",
    "FakeSignalStore",
    "FakeStore",
    "FakeUserRepository",
    "SentMessage",
    "fake_repositories",
    "member_account",
    "owner_account",
]
