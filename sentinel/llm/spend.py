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

**M10a makes it two-tier.** Each market has its own daily budget, and a global
ceiling sits above both. The sub-budgets deliberately sum to *more* than the
ceiling — 10 + 4 against 11 — so the markets compete for the last dollar instead of
each reserving one a quiet day would waste. Reaching a sub-budget suspends new deep
analysis in that market alone; reaching the ceiling suspends it everywhere.

Pure: no clock, no database. The caller supplies both.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sentinel.core.config import LLMConfig, MarketConfig


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


class SpendScope(StrEnum):
    """Which ceiling produced a verdict — this market's, or the deployment's.

    Carried rather than inferred, because the two call for different actions and
    read almost identically on a status card. "Crypto has spent its budget" is
    answered by raising that market's number; "the deployment has spent its budget"
    is answered by deciding which market matters more today.
    """

    MARKET = "market"
    GLOBAL = "global"


class SpendVerdict(BaseModel):
    """What today's spend means for the next deep-analysis call in one market."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: SpendState
    scope: SpendScope

    @property
    def suspends_analysis(self) -> bool:
        return self.state is SpendState.LIMIT_REACHED


def evaluate_market_spend(
    *,
    market_totals: SpendTotals,
    global_totals: SpendTotals,
    market: MarketConfig,
    global_limit_usd: Decimal,
    config: LLMConfig,
) -> SpendVerdict:
    """One market's verdict under both ceilings (M10a Step 4).

    Order, and why:

    1. **The global ceiling, first.** A sub-budget configured above it — which the
       shipped config does, on purpose — must never be able to mask it. Same reason
       :func:`evaluate_spend` checks its limit before its warn level: a
       misconfiguration has to fail safe.
    2. **This market's own limit.** Suspends this market and nothing else. A forex
       overspend must not stop the crypto book that is being measured, and the
       reverse must hold the day forex is enabled.
    3. **This market's warn level**, then the global one. The market's is the more
       actionable of the two, so it is reported when both are crossed.

    With one market enabled the two totals are the same number and the shipped
    warn levels are the same figure, so this returns exactly what
    :func:`evaluate_spend` returned before M10a existed — which is the point.
    """
    if global_totals.day_usd >= global_limit_usd:
        return SpendVerdict(state=SpendState.LIMIT_REACHED, scope=SpendScope.GLOBAL)
    if market_totals.day_usd >= market.llm_daily_budget_usd:
        return SpendVerdict(state=SpendState.LIMIT_REACHED, scope=SpendScope.MARKET)
    if market_totals.day_usd >= market.llm_daily_warn_usd:
        return SpendVerdict(state=SpendState.WARN, scope=SpendScope.MARKET)
    if global_totals.day_usd >= config.daily_spend_warn_usd:
        return SpendVerdict(state=SpendState.WARN, scope=SpendScope.GLOBAL)
    return SpendVerdict(state=SpendState.OK, scope=SpendScope.MARKET)


__all__ = [
    "SpendScope",
    "SpendState",
    "SpendTotals",
    "SpendVerdict",
    "evaluate_market_spend",
    "evaluate_spend",
    "spend_window",
]
