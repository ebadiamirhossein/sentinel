"""Turning ``signals`` rows into the population ``compute.py`` counts.

The split is the usual one: this module knows about Postgres, ``compute.py`` knows
about arithmetic, and neither knows about the other's problems. It is also the
only place the ``30d | 90d | all`` window is interpreted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.analyst.history import SetupStat
from sentinel.analyst.models import Direction, SetupType
from sentinel.bot.models import SignalDecision
from sentinel.core.logging import get_logger
from sentinel.stats.compute import by_key, split, summarize, tracked
from sentinel.stats.models import Population, ResolvedSignal, StatsReport, population_of
from sentinel.storage.models import SignalRow
from sentinel.storage.repositories import SignalRepository

log = get_logger(__name__)

WINDOWS = {"30d": 30, "90d": 90}
DEFAULT_WINDOW = "30d"


def window_start(window: str, *, now: datetime) -> datetime | None:
    """``None`` for "all" — the whole history, with no cutoff."""
    days = WINDOWS.get(window)
    return None if days is None else now - timedelta(days=days)


def parse_window(raw: str | None) -> str:
    """``/stats``'s optional argument. Anything unrecognised falls back to 30d."""
    if raw is None:
        return DEFAULT_WINDOW
    candidate = raw.strip().lower()
    return candidate if candidate in {*WINDOWS, "all"} else DEFAULT_WINDOW


def resolved_from_row(row: SignalRow) -> ResolvedSignal:
    """One row as a countable outcome.

    ``filled`` is the distinction everything downstream turns on: an expired or
    invalidated-before-entry signal is not a trade, and it is counted on its own
    line rather than as a 0R result.
    """
    decision = None if row.decision is None else SignalDecision(row.decision)
    return ResolvedSignal(
        signal_id=str(row.id),
        number=row.number,
        symbol=row.symbol,
        direction=Direction(row.direction),
        setup_type=SetupType(row.setup_type),
        prompt_version=row.prompt_version,
        population=population_of(decision, dry_run=row.dry_run),
        filled=row.filled_qty > 0,
        realized_r=row.realized_r or Decimal(0),
        realized_eur=row.realized_eur or Decimal(0),
        realized_costs_eur=row.realized_costs_eur or Decimal(0),
        reached_tp1=row.tp_hits > 0,
        outcome=row.outcome or "",
        closed_at=row.closed_at or row.created_at,
    )


class StatsRepository:
    """Reads the resolved book. Never commits, like every repository here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolved(self, *, since: datetime | None = None) -> list[ResolvedSignal]:
        rows = await SignalRepository(self._session).resolved_since(since)
        return [resolved_from_row(row) for row in rows]


async def build_report(session: AsyncSession, *, window: str, now: datetime) -> StatsReport:
    """Assemble everything ``/stats`` renders, in one pass over the window."""
    since = window_start(window, now=now)
    resolved = await StatsRepository(session).resolved(since=since)
    books = split(resolved)
    followed = tracked(resolved)

    return StatsReport(
        window=window,
        since=since,
        generated_at=now,
        real=summarize(books[Population.REAL]),
        hypothetical=summarize(books[Population.HYPOTHETICAL]),
        dry_run=summarize(books[Population.DRY_RUN]),
        by_setup=by_key(followed, "setup_type"),
        by_prompt_version=by_key(followed, "prompt_version"),
    )


async def setup_stats(
    session: AsyncSession, *, now: datetime, days: int = 30, minimum: int = 1
) -> list[SetupStat]:
    """specs/PROMPTS.md §3's "rolling 30-day stats per setup_type" for the prompt.

    Over real **plus hypothetical** and never the dry-run book: §3 calls this "your
    own past output and its measured performance", which is the analyst's record,
    not the owner's execution — a setup the owner skipped still tells the model
    whether the setup worked. A rehearsal day would be neither.

    ``float`` at this boundary only, because ``SetupStat`` has taken floats since
    M5 and this is a prompt line rather than money math.
    """
    resolved = await StatsRepository(session).resolved(since=now - timedelta(days=days))
    rows = by_key(tracked(resolved), "setup_type", minimum=minimum)
    return [
        SetupStat(
            setup_type=SetupType(row.key),
            count=row.stats.count,
            win_rate_pct=float(row.stats.win_rate_pct),
            avg_r=float(row.stats.avg_r),
        )
        for row in rows
        if row.stats.measured
    ]


__all__ = [
    "DEFAULT_WINDOW",
    "WINDOWS",
    "StatsRepository",
    "build_report",
    "parse_window",
    "resolved_from_row",
    "setup_stats",
    "window_start",
]
