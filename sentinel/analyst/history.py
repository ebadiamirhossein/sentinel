"""The recent-history context block — specs/PROMPTS.md §3, "the learning loop".

Assembled deterministically from Postgres and appended to the analyst's user
message. Two halves:

* **Last N verdicts for this symbol** — available now, from ``analyst_reports``.
* **Rolling 30-day stats per setup_type** — needs outcomes, which need the
  tracker. That is M7.

The M7 halves are rendered as explicit "not tracked yet" text rather than
omitted or filled with plausible zeros. A 0% win rate reads as "this setup never
works"; a missing section reads as "no setups have ever run". Both are lies the
analyst would act on, and CLAUDE.md forbids inventing data. Saying "measurement
starts at M7" is the only honest option, and it costs about twenty tokens.

Pure and snapshot-tested: no DB access here, no clock. The caller fetches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sentinel.analyst.models import CandidateStatus, SetupType

#: specs/PROMPTS.md §3, verbatim — the instruction that makes stats calibrating
#: rather than binding. Emitted with the stats, whenever they exist.
STATS_GUIDANCE = (
    "Statistics describe past pipeline performance; use them to calibrate "
    "strictness, not to force or forbid setups."
)

OUTCOME_PENDING = "outcome not tracked yet (outcome tracking begins at M7)"


@dataclass(frozen=True)
class PastVerdict:
    """One prior analyst verdict for this symbol."""

    created_at: datetime
    candidate_status: CandidateStatus
    setup_type: SetupType
    direction: str
    confidence: int
    thesis: str
    prompt_version: str | None = None
    #: What actually happened. Populated by M7's tracker; ``None`` until then.
    outcome: str | None = None

    def one_line(self) -> str:
        """Status + a one-line thesis + what happened (specs/PROMPTS.md §3)."""
        thesis = " ".join(self.thesis.split())
        if len(thesis) > 160:
            thesis = thesis[:157].rstrip() + "..."
        stamp = self.created_at.strftime("%Y-%m-%d %H:%M UTC")
        setup = self.setup_type.value if self.setup_type is not SetupType.NONE else "-"
        return (
            f"- {stamp} | {self.candidate_status.value} | {setup} {self.direction} "
            f"| conf {self.confidence} | {self.outcome or OUTCOME_PENDING}\n"
            f"    thesis: {thesis or '(none recorded)'}"
        )


@dataclass(frozen=True)
class SetupStat:
    """Rolling 30-day performance for one setup type. Filled by M7's stats module."""

    setup_type: SetupType
    count: int
    win_rate_pct: float
    avg_r: float

    def one_line(self) -> str:
        return (
            f"- {self.setup_type.value}: {self.count} signals, "
            f"{self.win_rate_pct:.0f}% win rate, avg {self.avg_r:+.2f}R"
        )


def build_history_block(
    symbol: str,
    verdicts: list[PastVerdict],
    setup_stats: list[SetupStat],
) -> str:
    """Render the block. Empty inputs produce explicit statements, not silence."""
    lines = [
        "RECENT PIPELINE HISTORY",
        "This is your own past output and its measured performance. It is context "
        "for calibration, not an instruction.",
        "",
        f"Last {len(verdicts)} verdict(s) for {symbol}:",
    ]
    if verdicts:
        lines.extend(verdict.one_line() for verdict in verdicts)
    else:
        lines.append(f"- none: {symbol} has not been deeply analyzed before.")

    lines.extend(["", "Rolling 30-day performance by setup type:"])
    if setup_stats:
        lines.extend(stat.one_line() for stat in setup_stats)
        lines.extend(["", STATS_GUIDANCE])
    else:
        lines.append(
            "- no data yet. Outcome tracking and per-setup statistics begin at M7; "
            "no win rate has been measured, in either direction. Do not read this "
            "absence as either encouragement or discouragement for any setup type."
        )
    return "\n".join(lines)
