"""Bot-side contracts (ARCHITECTURE.md §3 contract 5, specs/TELEGRAM_UX.md §2).

The M6 half of ``SignalRecord``: a gate-approved ``TradePlan``, the Telegram
message ids that delivered it, and the owner's decision. The tracker's half —
fills, TP/SL hits, realized R — lands at M7 and only adds fields.

M8.1 adds ``UserAccount`` and its two enums, plus ``user_id`` on ``SignalRecord``.
They live here rather than in a new module for the reason §7 of
journal/M6_REPORT.md records: ``storage/repositories.py`` translates these
contracts into rows, so whatever it imports from ``sentinel.bot`` must depend on
nothing but ``risk.models``. This module is that leaf, and it stays one.

No arithmetic and no LLM here: these are records of what happened.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from sentinel.risk.models import PauseState, TradePlan


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class UserStatus(StrEnum):
    """Where a Telegram id stands with this bot (M8.1).

    ``PENDING`` is created by ``/start`` and is the only state a stranger can put
    themselves in. ``REJECTED`` and ``SUSPENDED`` are the owner's decisions;
    ``LEFT`` is the user's own, via ``/leave`` — somebody who wants out must not
    have to ask permission to get out.

    Only ``APPROVED`` receives anything. The other four differ in *why* not, which
    is worth keeping distinct: a rejected stranger and a member who resigned are
    the same silence and completely different facts.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"
    LEFT = "LEFT"


class WatchlistRequestStatus(StrEnum):
    """Where a member's ``/request`` stands (M8.3).

    ``PENDING`` is the only state the database enforces uniqueness on, and it is
    deliberately also the state a request *stays* in when the owner tries to approve
    it but the watchlist has since filled up. That case is not a rejection — nobody
    decided against the symbol — so turning it into one would make the member ask
    again for something the owner already wanted.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class WatchlistRequest(Frozen):
    """One row of ``watchlist_requests``."""

    id: int
    symbol: str
    requested_by_user_id: int
    requested_at: datetime
    status: WatchlistRequestStatus = WatchlistRequestStatus.PENDING
    decided_at: datetime | None = None
    decided_by_user_id: int | None = None


class UserRole(StrEnum):
    """OWNER may approve, suspend and operate; MEMBER receives and decides.

    There is exactly one OWNER, and a partial unique index in the database says so
    — two of them would mean two people can admit users to someone else's system.
    """

    OWNER = "OWNER"
    MEMBER = "MEMBER"


#: Which wording of the first-run acknowledgement a user accepted. A disclaimer
#: somebody agreed to only means something if the words they saw are identifiable,
#: so the version is stored beside the timestamp — the same discipline
#: ``prompt_version`` applies to the analyst. Bump it when the text changes
#: materially, and every existing user is asked again.
ACK_VERSION = "v1"


class UserAccount(Frozen):
    """One row of ``users`` — identity, standing, and this user's own sizing.

    ``capital_eur`` and ``risk_per_trade_pct`` moved here from ``runtime_settings``
    at M8.1: they were one global value, and under multiple users they are the two
    numbers that must not be shared. ``None`` for either means "not set by this
    user" — capital then rejects with ``NO_CAPITAL`` exactly as before, and risk
    falls back to ``config.risk.risk_per_trade_pct``.

    ``pause`` is this user's **daily-loss** pause only (specs/RISK_ENGINE.md §7).
    The operator's ``/pause`` is a different thing and still lives in
    ``risk_state``, one row, gating everybody.
    """

    telegram_user_id: int
    status: UserStatus
    requested_at: datetime
    role: UserRole = UserRole.MEMBER
    username: str | None = None
    #: Telegram ``full_name``. Stored because a requester with no @username would
    #: otherwise reach the owner as a bare integer to approve or reject.
    display_name: str | None = None
    decided_at: datetime | None = None
    decided_by_user_id: int | None = None
    capital_eur: Decimal | None = None
    risk_per_trade_pct: Decimal | None = None
    acknowledged_at: datetime | None = None
    acknowledged_version: str = ""
    pause: PauseState = PauseState()
    #: When this id was last told where it stands, so a rejected stranger cannot
    #: make the bot hold a conversation.
    notice_at: datetime | None = None

    @property
    def is_owner(self) -> bool:
        return self.role is UserRole.OWNER

    @property
    def acknowledged(self) -> bool:
        """Accepted the *current* wording. A bumped ``ACK_VERSION`` asks again."""
        return self.acknowledged_at is not None and self.acknowledged_version == ACK_VERSION

    @property
    def capital_set(self) -> bool:
        return self.capital_eur is not None and self.capital_eur > 0

    def eligible_for_signals(self, now: datetime) -> bool:
        """May have a plan sized for them this cycle.

        Capital is deliberately **not** part of this: a user who is set up but has
        no capital still goes through the gate, is rejected with ``NO_CAPITAL``,
        and is told why. What capital does gate is whether a *deep analyst call*
        may be made on their behalf — see ``core/orchestrator.select_symbols``.
        """
        return (
            self.status is UserStatus.APPROVED
            and self.acknowledged
            and not self.pause.is_active(now)
        )


class SignalDecision(StrEnum):
    """specs/TELEGRAM_UX.md §2 — what the owner did with the card.

    The distinction is the whole point of the buttons: TAKEN outcomes are the
    owner's **real** statistics and consume the open-risk budget, WATCHING and
    SKIPPED are tracked as hypothetical. Skipped signals are still resolved in the
    background at M7, because what skipping costs is itself a measurement.
    """

    TAKEN = "TAKEN"
    WATCHING = "WATCHING"
    SKIPPED = "SKIPPED"


class SignalStatus(StrEnum):
    """ARCHITECTURE.md §3's tracker state machine.

    M6 only ever writes ``PENDING_ENTRY``; the rest of the vocabulary is declared
    here so M7's tracker extends a known enum instead of inventing a second one.
    """

    PENDING_ENTRY = "PENDING_ENTRY"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    STOPPED = "STOPPED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


#: The states the tracker never leaves. Everything else is still being followed.
#: Named here, beside the enum, so "is this signal still open?" has exactly one
#: definition — the tracker's loop, the dedup guard and /stats all ask it.
TERMINAL_STATUSES = frozenset(
    {
        SignalStatus.STOPPED,
        SignalStatus.INVALIDATED,
        SignalStatus.EXPIRED,
        SignalStatus.CLOSED,
    }
)

OPEN_STATUSES = frozenset(SignalStatus) - TERMINAL_STATUSES


class MessageKind(StrEnum):
    """Which message of a signal's thread this is (§6 idempotency key)."""

    CHARTS = "charts"
    CARD = "card"
    UPDATE = "update"


class MessageStatus(StrEnum):
    """PENDING is a *claim*, taken before the send — see ``TelegramMessageRow``."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class PostedMessage(Frozen):
    """One delivered (or claimed) Telegram message."""

    signal_id: UUID
    kind: MessageKind
    chat_id: int
    #: Which event within the kind (M7). ``""`` for the charts album and the card.
    event_key: str = ""
    message_id: int | None = None
    status: MessageStatus = MessageStatus.PENDING
    error: str | None = None


class SignalRecord(Frozen):
    """A plan as delivered: the numbers, the decision, and how to find the card."""

    signal_id: UUID = Field(default_factory=uuid4)
    #: Assigned by Postgres on insert; 0 until then, and only ever shown after.
    number: int = 0
    #: Whose signal this is (M8.1). One shared ``AnalystReport`` produces one row
    #: per approved user, each sized against that user's own capital, so every
    #: decision, fill, realized R and statistic downstream is already partitioned
    #: by the time anything reads it.
    user_id: int
    plan: TradePlan
    cycle_id: UUID | None = None
    status: SignalStatus = SignalStatus.PENDING_ENTRY
    decision: SignalDecision | None = None
    decided_at: datetime | None = None
    decided_by_user_id: int | None = None
    #: ``ChartRenderParams.to_json_dict()`` per attached chart (M3 §7).
    chart_params: tuple[dict[str, Any], ...] = ()
    #: Produced by a cycle running with ``dry_run: true`` (M7). Persisted and
    #: tracked exactly like a real signal, and never published: the tracker's
    #: notifier skips it, so a rehearsal day is completely silent while still
    #: producing a measured record. /stats keeps it in its own population.
    dry_run: bool = False


__all__ = [
    "ACK_VERSION",
    "OPEN_STATUSES",
    "TERMINAL_STATUSES",
    "MessageKind",
    "MessageStatus",
    "PostedMessage",
    "SignalDecision",
    "SignalRecord",
    "SignalStatus",
    "UserAccount",
    "UserRole",
    "UserStatus",
]
