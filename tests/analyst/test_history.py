"""The recent-history block (specs/PROMPTS.md §3), including its M7 stubs."""

from __future__ import annotations

from datetime import UTC, datetime

from sentinel.analyst.history import (
    OUTCOME_PENDING,
    STATS_GUIDANCE,
    PastVerdict,
    SetupStat,
    build_history_block,
)
from sentinel.analyst.models import CandidateStatus, SetupType


def verdict(**overrides: object) -> PastVerdict:
    base: dict[str, object] = {
        "created_at": datetime(2026, 8, 18, 9, 0, tzinfo=UTC),
        "candidate_status": CandidateStatus.CANDIDATE,
        "setup_type": SetupType.TREND_PULLBACK,
        "direction": "long",
        "confidence": 78,
        "thesis": "4h uptrend intact; 1h pullback into demand.",
    }
    base.update(overrides)
    return PastVerdict(**base)  # type: ignore[arg-type]


def test_empty_history_says_so_explicitly() -> None:
    """Silence would read as "nothing ever worked"; both are wrong."""
    block = build_history_block("SOLUSDT", [], [])
    assert "has not been deeply analyzed before" in block
    assert "SOLUSDT" in block


def test_missing_stats_say_nothing_was_measured_rather_than_zero() -> None:
    """A fabricated 0% win rate is a lie the analyst would act on (CLAUDE.md).

    Through M5 and M6 this branch said "measurement begins at M7". It does now, so
    the wording is about the *window* — a fresh database, or thirty quiet days —
    and no longer about a milestone.
    """
    block = build_history_block("SOLUSDT", [verdict()], [])
    assert "no outcomes resolved yet in the last 30 days" in block
    assert "either encouragement or discouragement" in block
    assert "0%" not in block
    assert "M7" not in block, "the milestone stub must be gone, not reworded"


def test_an_unresolved_outcome_is_marked_pending_not_invented() -> None:
    """A signal still running has no result to report, and reporting the R it
    happens to be showing would teach the analyst to read an unrealised number as
    a measurement."""
    block = build_history_block("SOLUSDT", [verdict()], [])
    assert OUTCOME_PENDING in block
    assert "M7" not in OUTCOME_PENDING


def test_a_resolved_outcome_is_rendered_on_the_verdict_line() -> None:
    """specs/PROMPTS.md §3 asks for "status + one-line thesis + what happened".
    From M7 the third part is real — this is the learning loop closing."""
    block = build_history_block("SOLUSDT", [verdict(outcome="stop, -0.40R")], [])
    assert "stop, -0.40R" in block
    assert OUTCOME_PENDING not in block


def test_real_setup_stats_render_with_the_calibration_instruction() -> None:
    """§3 requires the guidance sentence to travel *with* the numbers: statistics
    calibrate strictness, they do not force or forbid a setup."""
    block = build_history_block(
        "SOLUSDT",
        [verdict()],
        [SetupType_stat()],
    )
    assert "trend_pullback: 12 signals, 58% win rate, avg +0.31R" in block
    assert STATS_GUIDANCE in block


def SetupType_stat() -> SetupStat:
    return SetupStat(setup_type=SetupType.TREND_PULLBACK, count=12, win_rate_pct=58.0, avg_r=0.31)


def test_verdict_line_carries_status_setup_and_thesis() -> None:
    block = build_history_block("SOLUSDT", [verdict()], [])
    assert "CANDIDATE" in block
    assert "trend_pullback long" in block
    assert "conf 78" in block
    assert "4h uptrend intact" in block


def test_long_thesis_is_trimmed_to_one_line() -> None:
    block = build_history_block("SOLUSDT", [verdict(thesis="word " * 200)], [])
    thesis_line = next(line for line in block.splitlines() if "thesis:" in line)
    assert len(thesis_line) < 200
    assert thesis_line.rstrip().endswith("...")


def test_newlines_in_a_stored_thesis_cannot_break_the_block() -> None:
    block = build_history_block("SOLUSDT", [verdict(thesis="line one\nline two")], [])
    assert "line one line two" in block


def test_stats_carry_the_spec_s_calibration_sentence() -> None:
    """specs/PROMPTS.md §3: stats calibrate strictness, they do not command."""
    stats = [SetupStat(SetupType.BREAKOUT_RETEST, count=12, win_rate_pct=41.0, avg_r=-0.1)]
    block = build_history_block("SOLUSDT", [verdict()], stats)
    assert STATS_GUIDANCE in block
    assert "breakout_retest: 12 signals, 41% win rate, avg -0.10R" in block


def test_ordering_is_verdicts_then_stats() -> None:
    stats = [SetupStat(SetupType.MEAN_REVERSION, count=3, win_rate_pct=66.0, avg_r=0.8)]
    block = build_history_block("SOLUSDT", [verdict()], stats)
    assert block.index("Last 1 verdict") < block.index("Rolling 30-day")
