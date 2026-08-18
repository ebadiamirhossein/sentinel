"""The analyst's output contract (specs/PROMPTS.md §2).

Defined here in M4 — ahead of the analyst itself — because the risk engine is the
gate that consumes it, and a contract duplicated in two modules is a contract that
will drift. M5 fills these models from the LLM's structured output; nothing in
this file knows anything about an LLM.

**No sizing fields exist here by design** (ARCHITECTURE.md §3, contract 3): the
analyst never sizes, never sets leverage, never sees EUR.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CandidateStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    WATCHLIST = "WATCHLIST"
    NO_SETUP = "NO_SETUP"


class SetupType(StrEnum):
    TREND_PULLBACK = "trend_pullback"
    RANGE_REVERSAL = "range_reversal"
    BREAKOUT_RETEST = "breakout_retest"
    MOMENTUM_CONTINUATION = "momentum_continuation"
    MEAN_REVERSION = "mean_reversion"
    NONE = "none"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class TimeframeLabel(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"
    NONE = "none"


class EntryZone(Frozen):
    """The analyst supplies a zone; the risk engine decides the ladder inside it."""

    low: Decimal
    high: Decimal

    @property
    def width(self) -> Decimal:
        return self.high - self.low

    @property
    def midpoint(self) -> Decimal:
        return (self.low + self.high) / Decimal(2)


class Evidence(Frozen):
    """One claim, tied to the snapshot field that supports it (PROMPTS.md §2, rule 3)."""

    claim: str
    source_field: str


class AnalystReport(Frozen):
    """One symbol, one deep analysis. Schema-validated before it reaches the gate."""

    schema_version: int = 1
    symbol: str
    candidate_status: CandidateStatus
    setup_type: SetupType = SetupType.NONE
    direction: Direction = Direction.NONE
    timeframe_label: TimeframeLabel = TimeframeLabel.NONE

    thesis: str = Field(default="", max_length=600)
    evidence: tuple[Evidence, ...] = ()
    counter_thesis: str = Field(default="", max_length=300)

    entry_zone: EntryZone | None = None
    stop: Decimal | None = None
    targets: tuple[Decimal, ...] = ()
    invalidation_price: Decimal | None = None
    invalidation_text: str = Field(default="", max_length=200)

    confidence: int = Field(default=0, ge=0, le=100)
    data_quality_note: str | None = None

    #: Which prompt produced this — the A/B key for /stats (PROMPTS.md §5).
    prompt_version: str | None = None
    model: str | None = None
