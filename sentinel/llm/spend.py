"""The LLM spend guard (M7 — pulled forward from M8's "spend guard" item).

M7 is the first milestone where the scheduler runs unattended, which is exactly
when a bug — or a market event that makes every symbol look interesting — can
spend real money between midnight and breakfast. So the brake ships with the
scheduler rather than after it.

**What it stops, and what it must never stop.** Reaching the daily limit suspends
*new deep analysis*: the expensive tier, and the only one whose volume a bad day
can multiply. The screener keeps triaging, because its whole batch is ~$0.023 and
a blind cycle is worse than a cheap one. The tracker keeps running unconditionally
— it manages open positions and makes no LLM calls at all, and a guard that could
stop it would abandon a live trade to protect a dollar.

**It self-clears.** The totals are recomputed from ``llm_calls`` every cycle, so
the suspension lapses at 00:00 UTC on its own. There is no pause row to get stuck
in and nothing to ``/resume`` — unlike the daily-loss rail, where the persistence
*is* the point.

**It gates an estimate, and says so.** ``cost_usd_estimate`` is derived from token
counts and ``config.llm.pricing``, not from a bill. Worse, ``pricing.estimate_cost``
prices an *unknown* model at 0 with a warning — right for keeping the audit row,
and a hole here, because an unpriced model would spend invisibly. So the totals
carry ``unpriced_calls`` and every figure derived from them is reported as a floor
when that count is non-zero, in the same spirit as ``funding n/a`` on a card.

Pure: no clock, no database. The caller supplies both.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sentinel.core.config import LLMConfig


class SpendState(StrEnum):
    """What today's spend means for the next deep-analysis call."""

    OK = "OK"
    #: Past the warn level — one Telegram notice, nothing suspended.
    WARN = "WARN"
    #: At or past the daily limit — no new deep analysis until 00:00 UTC.
    LIMIT_REACHED = "LIMIT_REACHED"


class SpendTotals(BaseModel):
    """Accumulated ``cost_usd_estimate`` over the current UTC day and month."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day_usd: Decimal = Decimal("0")
    month_usd: Decimal = Decimal("0")
    calls: int = 0
    #: Calls whose model is absent from ``config.llm.pricing``. They were priced at
    #: 0 and are therefore *missing* from the totals, not free.
    unpriced_calls: int = 0

    @property
    def is_floor(self) -> bool:
        """True when the totals understate reality by an unknown amount."""
        return self.unpriced_calls > 0

    @property
    def priced_calls(self) -> int:
        return self.calls - self.unpriced_calls


def spend_window(now: datetime) -> tuple[datetime, datetime]:
    """``(start of today, start of this month)`` in UTC.

    The owner ruling for M7 puts every "daily" boundary on the UTC calendar day,
    the same instant every stored row and log line is stamped against, so a
    suspension can always be reconciled with the audit trail by eye.
    """
    if now.tzinfo is None:
        raise ValueError("spend_window needs an aware UTC datetime — everything internal is UTC")
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day.replace(day=1)


def evaluate_spend(totals: SpendTotals, config: LLMConfig) -> SpendState:
    """Today's verdict. The limit is checked first, so a warn level configured
    above the limit cannot mask it — a misconfiguration must fail safe.

    ``>=`` and not ``>``: $10.00 against a $10 limit is the limit reached. Waiting
    for one more cent would let a run of cheap calls idle at the ceiling.
    """
    if totals.day_usd >= config.daily_spend_limit_usd:
        return SpendState.LIMIT_REACHED
    if totals.day_usd >= config.daily_spend_warn_usd:
        return SpendState.WARN
    return SpendState.OK


__all__ = ["SpendState", "SpendTotals", "evaluate_spend", "spend_window"]
