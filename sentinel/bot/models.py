"""Bot-side contracts (ARCHITECTURE.md §3 contract 5, specs/TELEGRAM_UX.md §2).

The M6 half of ``SignalRecord``: a gate-approved ``TradePlan``, the Telegram
message ids that delivered it, and the owner's decision. The tracker's half —
fills, TP/SL hits, realized R — lands at M7 and only adds fields.

No arithmetic and no LLM here: these are records of what happened.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from sentinel.risk.models import TradePlan


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


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
    message_id: int | None = None
    status: MessageStatus = MessageStatus.PENDING
    error: str | None = None


class SignalRecord(Frozen):
    """A plan as delivered: the numbers, the decision, and how to find the card."""

    signal_id: UUID = Field(default_factory=uuid4)
    #: Assigned by Postgres on insert; 0 until then, and only ever shown after.
    number: int = 0
    plan: TradePlan
    cycle_id: UUID | None = None
    status: SignalStatus = SignalStatus.PENDING_ENTRY
    decision: SignalDecision | None = None
    decided_at: datetime | None = None
    decided_by_user_id: int | None = None
    #: ``ChartRenderParams.to_json_dict()`` per attached chart (M3 §7).
    chart_params: tuple[dict[str, Any], ...] = ()


__all__ = [
    "MessageKind",
    "MessageStatus",
    "PostedMessage",
    "SignalDecision",
    "SignalRecord",
    "SignalStatus",
]
