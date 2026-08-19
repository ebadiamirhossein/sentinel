"""The failure-streak rule — ARCHITECTURE.md §2 and §6, one table.

The alert is the only thing that will ever tell the owner the pipeline stopped
while they were asleep, and it is also the only thing that can wake them at 3am
for nothing. Both directions are failures, so the rule is tested as a table over
streak lengths rather than as a handful of interesting cases, and a meta-test
proves the table can fail (M6 §3's rule: a guard that cannot fail is not a guard).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sentinel.core.alerts import (
    Alert,
    AlertKind,
    CycleOutcome,
    consecutive_failures,
    cycle_alert,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
#: 2 x the 15-minute scan interval, as core/app.py computes it from config.
STALE = timedelta(minutes=30)


def rows(*statuses: str, gap_minutes: int = 15) -> list[CycleOutcome]:
    """Cycle rows newest-first, one scan interval apart, as the repository returns."""
    return [
        CycleOutcome(
            status=status,
            started_at=NOW - timedelta(minutes=gap_minutes * (index + 1)),
            finished_at=None
            if status == "RUNNING"
            else NOW - timedelta(minutes=gap_minutes * index),
            error="ScreenerUnavailable: 529 overloaded" if status == "FAILED" else None,
        )
        for index, status in enumerate(statuses)
    ]


def alert(*statuses: str, threshold: int = 3) -> Alert | None:
    return cycle_alert(rows(*statuses), now=NOW, stale_after=STALE, threshold=threshold)


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        # Nothing has happened yet, or nothing is wrong.
        ((), None),
        (("OK",), None),
        (("OK", "OK", "OK", "OK"), None),
        # Below the threshold: silence. Two failures is a bad afternoon, not an
        # outage, and a system that messages on every one gets muted.
        (("FAILED",), None),
        (("FAILED", "FAILED"), None),
        (("FAILED", "FAILED", "OK"), None),
        # The threshold itself.
        (("FAILED", "FAILED", "FAILED"), AlertKind.CYCLE_FAILURES),
        (("FAILED", "FAILED", "FAILED", "OK", "OK"), AlertKind.CYCLE_FAILURES),
        # Past it: quiet again until the next multiple, so an outage lasting all
        # night says so four times rather than forty.
        (("FAILED",) * 4, None),
        (("FAILED",) * 5, None),
        (("FAILED",) * 6, AlertKind.CYCLE_FAILURES),
        (("FAILED",) * 7, None),
        (("FAILED",) * 9, AlertKind.CYCLE_FAILURES),
        # Recovery, once, and only for a streak that was announced.
        (("OK", "FAILED", "FAILED", "FAILED"), AlertKind.CYCLE_RECOVERED),
        (("OK", "OK", "FAILED", "FAILED", "FAILED"), None),
        (("OK", "FAILED", "FAILED"), None),
        (("OK", "FAILED"), None),
    ],
)
def test_the_streak_rule(statuses: tuple[str, ...], expected: AlertKind | None) -> None:
    result = alert(*statuses)
    assert (None if result is None else result.kind) == expected


def test_the_table_would_catch_a_wrong_rule() -> None:
    """Proof the assertions above can fail — in both directions.

    Without this, a ``cycle_alert`` that always returned ``None`` would pass every
    silent row of the table and fail only the loud ones, and a change that made it
    over-eager would be caught by nothing at all.
    """
    assert alert("FAILED", "FAILED", "FAILED") is not None
    assert alert("FAILED", "FAILED") is None


# --------------------------------------------------------------------------- #
# What counts as a failure
# --------------------------------------------------------------------------- #


def test_a_cycle_still_running_inside_its_budget_is_not_a_failure() -> None:
    """The scan job writes RUNNING before it starts. That is not bad news."""
    fresh = CycleOutcome(status="RUNNING", started_at=NOW - timedelta(minutes=1))
    assert consecutive_failures([fresh], now=NOW, stale_after=STALE) == 0


def test_a_cycle_running_in_front_of_a_streak_does_not_reset_it() -> None:
    """The cycle in flight is the fourth failure about to be written.

    If an in-progress row broke the streak, the alert would never fire at all:
    every evaluation happens with a RUNNING row at the top.
    """
    in_flight = [CycleOutcome(status="RUNNING", started_at=NOW - timedelta(minutes=1))]
    result = cycle_alert(
        in_flight + rows("FAILED", "FAILED", "FAILED"),
        now=NOW,
        stale_after=STALE,
        threshold=3,
    )
    assert result is not None
    assert result.kind is AlertKind.CYCLE_FAILURES


def test_a_stale_running_row_counts_as_a_failure_and_says_why() -> None:
    """A process killed mid-cycle leaves RUNNING for ever — the worst case, and
    the one no status column would otherwise report."""
    killed = [
        CycleOutcome(status="RUNNING", started_at=NOW - timedelta(hours=2)),
        *rows("FAILED", "FAILED"),
    ]
    result = cycle_alert(killed, now=NOW, stale_after=STALE, threshold=3)
    assert result is not None
    assert result.failures == 3
    assert result.last_error is not None
    assert "died mid-cycle" in result.last_error


def test_the_alert_carries_the_newest_error_and_the_oldest_timestamp() -> None:
    """What broke, and since when — the two questions a 3am message must answer."""
    result = alert("FAILED", "FAILED", "FAILED")
    assert result is not None
    assert result.failures == 3
    assert result.last_error == "ScreenerUnavailable: 529 overloaded"
    assert result.since == NOW - timedelta(minutes=45)


def test_the_threshold_is_configurable_and_must_be_positive() -> None:
    assert alert("FAILED", threshold=1) is not None
    assert alert("FAILED", "FAILED", threshold=5) is None
    with pytest.raises(ValueError, match="at least 1"):
        alert("FAILED", threshold=0)
