"""Screener contracts (ARCHITECTURE.md §3 contract 2, specs/PROMPTS.md §1)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sentinel.llm.models import LLMCall


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DirectionHint(StrEnum):
    LONG = "long"
    SHORT = "short"
    UNCLEAR = "unclear"


class ScreenerVerdict(Frozen):
    """One symbol's triage result. ``reason`` is capped at 200 chars by §1."""

    symbol: str
    interesting: bool
    direction_hint: DirectionHint
    reason: str = Field(max_length=200)


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
