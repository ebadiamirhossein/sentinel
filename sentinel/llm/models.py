"""The audit record for one LLM request/response pair (PRD F10, G5).

Every call is recorded — successful, refused, malformed or failed — because M9
asks "what is the pipeline actually spending, and on what?", and a table that
only holds successes cannot answer it.

Money is ``Decimal`` (CLAUDE.md). Image inputs are recorded as
``{sha256, params}`` references rather than bytes: M3 proved a chart re-renders
byte-identically from stored OHLCV, so PRD F4's reconstruction requirement holds
without putting ~400KB of PNG in Postgres on every analyst call.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LLMCallKind(StrEnum):
    """Which pipeline stage made the call (ARCHITECTURE.md §1)."""

    SCREENER = "SCREENER"
    ANALYST = "ANALYST"


class LLMCallStatus(StrEnum):
    OK = "OK"
    #: Response was not valid against the schema (after this attempt).
    INVALID_JSON = "INVALID_JSON"
    #: stop_reason == "refusal" — safety classifiers declined.
    REFUSAL = "REFUSAL"
    #: Ran out of output budget mid-answer; the content is unusable.
    TRUNCATED = "TRUNCATED"
    TIMEOUT = "TIMEOUT"
    API_ERROR = "API_ERROR"


class TokenUsage(Frozen):
    """Token counts as reported by the API. The ground truth behind the cost."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class LLMCall(Frozen):
    """One request/response pair, exactly as it happened."""

    schema_version: int = 1
    call_id: UUID = Field(default_factory=uuid4)
    cycle_id: UUID | None = None
    #: Null for the screener, which is a batch call over the whole watchlist.
    symbol: str | None = None

    kind: LLMCallKind
    provider: str
    model: str
    prompt_version: str
    #: 1 for the first try; 2 for the single schema retry (PROMPTS §2).
    attempt: int = 1

    status: LLMCallStatus
    stop_reason: str | None = None
    refusal_category: str | None = None
    error: str | None = None

    usage: TokenUsage = TokenUsage()
    #: Derived from `usage` and `config.llm.pricing`. An estimate, by name.
    cost_usd_estimate: Decimal = Decimal("0")
    duration_ms: int = 0
    #: Anthropic's `request-id` header — quote it when reporting an API problem.
    request_id: str | None = None

    started_at: datetime
    #: Request as sent, with images as {sha256, params} references (see module doc).
    request: dict[str, Any] = {}
    #: Response text/JSON as received. Never truncated — this is the audit trail.
    response: dict[str, Any] = {}
