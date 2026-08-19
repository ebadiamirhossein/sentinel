"""Screener contracts (ARCHITECTURE.md §3 contract 2, specs/PROMPTS.md §1)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from sentinel.llm.models import LLMCall
from sentinel.llm.schema import capped


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DirectionHint(StrEnum):
    LONG = "long"
    SHORT = "short"
    UNCLEAR = "unclear"


#: Truncates instead of rejecting — see the comment on ``reason`` below.
Reason = Annotated[str, BeforeValidator(capped(200, field="reason"))]


class ScreenerVerdict(Frozen):
    """One symbol's triage result. ``reason`` is capped at 200 chars by §1."""

    # The cap TRUNCATES rather than rejecting, and the description states it -- both
    # halves of M8.2 #4's lesson, which this model was missing until M8.3 found it the
    # expensive way. `llm.schema` strips `maxLength` from the wire schema, so a bare
    # `max_length` told the model nothing and discarded the WHOLE BATCH when one
    # verdict of ten ran long: 3 of 8 live batches on 2026-08-19, each costing a retry.
    # It also became *more* likely when `screener_v2` started asking the reason to name
    # what changed -- a prompt asking for more prose in a silently capped field is a
    # defect waiting on a verbose day. This is a comment and not a docstring because a
    # model docstring is emitted as the schema `description` and sent on every call.
    symbol: str
    interesting: bool
    direction_hint: DirectionHint
    reason: Reason = Field(
        max_length=200,
        description=(
            "Why, in at most 200 characters. Name the specific trigger; an over-long "
            "reason is truncated."
        ),
    )


class ScreenerBatch(Frozen):
    """The wire wrapper.

    specs/PROMPTS.md §1 asks for a bare JSON array, but structured outputs
    requires an object at the top level, so the array is nested under one key.
    The per-symbol shape §1 specifies is unchanged.
    """

    verdicts: tuple[ScreenerVerdict, ...]


class ScreenerResult(Frozen):
    """What the cycle gets back: reconciled verdicts plus the audit records.

    ``verdicts`` is always one entry per symbol submitted, in the order
    submitted, whatever the model returned -- see ``screener.reconcile``.
    ``calls`` holds every attempt, including discarded ones (PRD F10).
    """

    verdicts: tuple[ScreenerVerdict, ...] = ()
    calls: tuple[LLMCall, ...] = ()
    #: True when no usable batch came back and every symbol defaulted to
    #: not-interesting. The cycle continues with zero candidates.
    degraded: bool = False

    @property
    def interesting(self) -> tuple[ScreenerVerdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.interesting)
