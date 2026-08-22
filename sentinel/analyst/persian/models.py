"""The cross-module contract for a stored Persian summary (CLAUDE.md §Code standards).

One model, defined once, shared by the repository that persists it, the summariser
that produces it and the handler that sends it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sentinel.core.markets import Market


class PersianSourceKind(StrEnum):
    """Which card this summarises, and therefore which id column carries its origin."""

    #: A delivered signal card. ``signal_id`` is set.
    SIGNAL = "signal"
    #: A ``/pulse SYMBOL`` analyst verdict card. ``analyst_report_id`` is set.
    PULSE_VERDICT = "pulse_verdict"


class PersianSummary(BaseModel):
    """One Persian rewrite of one card, as stored.

    ``input_sha256`` is the identity: identical input text is the same summary, whoever
    pressed the button and whichever card kind it came from. See
    :class:`sentinel.storage.models.PersianSummaryRow` for why the signal id is not.

    ``summary_text`` is what the model returned, **without** the reference line. That
    line is appended at render time, so its wording can change without a migration and
    without leaving old rows carrying a superseded sentence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_sha256: str
    source_kind: PersianSourceKind
    signal_id: UUID | None = None
    analyst_report_id: UUID | None = None
    market: Market
    symbol: str
    summary_text: str
    input_text: str
    prompt_version: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd_estimate: Decimal = Decimal("0")
    llm_call_id: UUID | None = None
    created_at: datetime
    created_by_user_id: int


__all__ = ["PersianSourceKind", "PersianSummary"]
