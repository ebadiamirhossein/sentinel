"""What a measured outcome is, and what a set of them adds up to.

PRD G2: "Win rate, average R, profit factor, and max drawdown are computable at
any time via /stats. The system's real success rate is *known*, not guessed."

Three populations, reported separately and never merged, because they answer
three different questions:

* **REAL** — the owner pressed ✅ Taken. This is their record.
* **HYPOTHETICAL** — 👀 Watching or ❌ Skip. specs/TELEGRAM_UX.md §2 resolves these
  too, "because what skipping costs is itself a measurement".
* **DRY RUN** — produced by a rehearsal cycle and never published. Nobody pressed
  anything, so folding these into either of the above would put paper results
  into the numbers the live system is later judged against.

No LLM (CLAUDE.md's deterministic modules). ``Decimal`` throughout; the only
``float`` in the module is at ``SetupStat``'s boundary, which M5 defined.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sentinel.analyst.models import Direction, SetupType
from sentinel.bot.models import SignalDecision


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Population(StrEnum):
    REAL = "REAL"
    HYPOTHETICAL = "HYPOTHETICAL"
    DRY_RUN = "DRY_RUN"


def population_of(decision: SignalDecision | None, *, dry_run: bool) -> Population:
    """Which book a resolved signal belongs to.

    ``dry_run`` wins over the decision. A rehearsal signal can carry a decision
    only if someone pressed a button on a card that was never sent, which cannot
    happen — but if it ever did, it would still not be a real trade.
    """
    if dry_run:
        return Population.DRY_RUN
    if decision is SignalDecision.TAKEN:
        return Population.REAL
    return Population.HYPOTHETICAL


class ResolvedSignal(Frozen):
    """One signal with a measured outcome, flattened for counting."""

    signal_id: str
    number: int
    symbol: str
    direction: Direction
    setup_type: SetupType
    prompt_version: str | None
    population: Population
    #: False for a signal that expired or invalidated before any rung filled.
    filled: bool
    realized_r: Decimal
    realized_eur: Decimal
    realized_costs_eur: Decimal
    #: PRD G3's target is phrased as "reach at least TP1 or breakeven".
    reached_tp1: bool
    outcome: str
    closed_at: datetime


class PerformanceStats(Frozen):
    """The figures specs/TELEGRAM_UX.md §3 asks ``/stats`` for.

    ``profit_factor`` is ``None`` — not infinity, and not zero — when there are no
    losses yet. A ratio with an empty denominator is undefined, and rendering it
    as a number would read as a measured result on a sample of one good week.
    """

    count: int = 0
    filled: int = 0
    unfilled: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    win_rate_pct: Decimal = Decimal("0")
    avg_r: Decimal = Decimal("0")
    total_r: Decimal = Decimal("0")
    total_eur: Decimal = Decimal("0")
    costs_eur: Decimal = Decimal("0")
    profit_factor: Decimal | None = None
    max_drawdown_r: Decimal = Decimal("0")
    reached_tp1: int = 0
    reached_tp1_pct: Decimal = Decimal("0")

    @property
    def measured(self) -> bool:
        """False when there is nothing to report — say so rather than print 0%."""
        return self.filled > 0


class Breakdown(Frozen):
    """One row of a by-setup or by-prompt-version table."""

    key: str
    stats: PerformanceStats


class StatsReport(Frozen):
    """Everything ``/stats [30d|90d|all]`` renders."""

    window: str
    since: datetime | None
    generated_at: datetime
    real: PerformanceStats
    hypothetical: PerformanceStats
    dry_run: PerformanceStats
    #: Over REAL + HYPOTHETICAL together. With <=5 signals a day the taken-only
    #: sample is too thin to split by setup type for months, and the question a
    #: breakdown answers — "which setups does the *pipeline* get right" — is about
    #: the analyst, not about which cards the owner chose to act on.
    by_setup: tuple[Breakdown, ...] = ()
    by_prompt_version: tuple[Breakdown, ...] = ()


__all__ = [
    "Breakdown",
    "PerformanceStats",
    "Population",
    "ResolvedSignal",
    "StatsReport",
    "population_of",
]
