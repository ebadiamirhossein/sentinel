"""Deciding when a run of failures deserves a message — ARCHITECTURE.md §2 and §6.

Both sections say the same thing: "Telegram admin alert after **3 consecutive
cycle failures**". This module is that rule, and nothing else. It holds no clock,
no database and no bot; ``sentinel/bot/alerts.py`` does the sending. Same split as
the tracker and its notifier, for the same two reasons — CLAUDE.md forbids a
sideways import between pipeline stages, and a pure function can be exercised over
every streak length in a table rather than by arranging an outage.

Three rulings the specs do not make, recorded here because an alerting rule that
is only in someone's head is one that drifts (journal/M8_REPORT.md §3):

**A stale ``RUNNING`` row counts as a failure.** ``cycles.status`` is written
``RUNNING`` before a cycle starts and rewritten at the end, so a process killed
mid-cycle leaves ``RUNNING`` behind for ever. That *is* a failed cycle — the most
serious kind, since it means the process died rather than the cycle. A RUNNING row
younger than ``stale_after`` is simply the cycle in progress and is ignored.

**The re-alert cadence is a multiple of the threshold**, so a continuing outage
says so again every third failure rather than once and then silence. This is
computed from the rows themselves — no "last alerted at" state anywhere — which is
what makes it correct across a restart, and is the reason M8 needs no migration.

**Recovery is announced once.** The first OK cycle after a qualifying streak
sends one notice. Without it, the last thing the phone ever said about an outage
is that it was still broken.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

#: ARCHITECTURE.md §2 / §6. Overridable via ``config.yaml``'s ``alerts:`` block.
DEFAULT_THRESHOLD = 3


class AlertKind(StrEnum):
    """What an admin alert is about."""

    CYCLE_FAILURES = "CYCLE_FAILURES"
    CYCLE_RECOVERED = "CYCLE_RECOVERED"
    #: The M7 spend guard crossing its warn level (specs: journal/M7_REPORT.md §9).
    SPEND_WARN = "SPEND_WARN"
    #: The guard reaching the daily limit — new deep analysis is now suspended.
    SPEND_LIMIT = "SPEND_LIMIT"
    #: M10b. The Saxo refresh chain is dead and only a manual browser login can
    #: restore it (specs/FOREX.md §3.1). Its own kind, not a generic "forex paused",
    #: because the two call for completely different owner actions: one is a data
    #: problem to investigate and the other is a two-minute login. An alert that does
    #: not say which sends the owner looking in the wrong place at the worst time.
    FOREX_REAUTH_REQUIRED = "FOREX_REAUTH_REQUIRED"
    #: M10d. The hand-maintained economic calendar is running out, or has run out
    #: (specs/FOREX.md §8). Its own kind for the same reason the one above has one:
    #: the owner action is specific — extend one YAML file and rebuild the image —
    #: and an alert that does not say so is an alert that gets read and not acted on.
    #: There is no second source, so a lapsed calendar suppresses every forex signal;
    #: this is the message that arrives BEFORE that happens.
    FOREX_CALENDAR_COVERAGE = "FOREX_CALENDAR_COVERAGE"
    #: M10e. The market was open, we looked, and nothing came back — for N cycles in a
    #: row. Its own kind and not CYCLE_FAILURES, because nothing failed: every one of
    #: those cycles reported ``status: OK``, spent $0.00 and left /health green. That is
    #: the shape of this defect and the reason it ran for a day unnoticed, so the alert
    #: has to be about ingestion producing nothing rather than about anything raising.
    MARKET_BARREN = "MARKET_BARREN"


@dataclass(frozen=True)
class CycleOutcome:
    """The four columns of a ``cycles`` row this decision needs."""

    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True)
class Alert:
    """One message to send, with everything the card renders already decided."""

    kind: AlertKind
    #: How many cycles in the current failing run. 0 for a recovery or a spend alert.
    failures: int = 0
    #: The newest failure's ``error``, verbatim and already truncated by the
    #: orchestrator to 512 chars. ``None`` when the cycle left no message — a
    #: killed process does not get to write one.
    last_error: str | None = None
    #: When the newest counted failure started. Anchors the alert in time for an
    #: owner reading it hours later.
    since: datetime | None = None
    detail: str = ""
    #: What the owner has to **do**, one line each, in the order it matters. Empty
    #: for the alerts whose action is "look at the logs". A status without an action
    #: is the failure §3.1 names: it sends the reader hunting the wrong problem.
    actions: tuple[str, ...] = ()


def _failed(outcome: CycleOutcome, *, now: datetime, stale_after: timedelta) -> bool:
    if outcome.status == "FAILED":
        return True
    if outcome.status == "RUNNING":
        return now - outcome.started_at > stale_after
    return False


def consecutive_failures(
    rows: Sequence[CycleOutcome], *, now: datetime, stale_after: timedelta
) -> int:
    """How many cycles have failed in a row, newest first.

    A cycle still running inside its own budget is neither a success nor a
    failure: it is not counted, and it does **not** break the streak either. An
    in-flight cycle must not reset the count that a fourth failure is about to
    reach.
    """
    streak = 0
    for outcome in rows:
        if _failed(outcome, now=now, stale_after=stale_after):
            streak += 1
        elif outcome.status == "RUNNING":
            continue
        else:
            break
    return streak


def cycle_alert(
    rows: Sequence[CycleOutcome],
    *,
    now: datetime,
    stale_after: timedelta,
    threshold: int = DEFAULT_THRESHOLD,
) -> Alert | None:
    """The alert this run of cycles justifies, or ``None`` for silence.

    ``rows`` is newest-first, as ``CycleRepository.recent`` returns them. Pass at
    least ``threshold + 1`` of them or a recovery cannot be recognised.
    """
    if threshold < 1:
        raise ValueError("alert threshold must be at least 1 cycle")

    streak = consecutive_failures(rows, now=now, stale_after=stale_after)
    if streak >= threshold:
        # 3, 6, 9 … — the first alert, then one every `threshold` further
        # failures. Anything else needs a "last alerted" row somewhere, and a
        # restart would either duplicate it or lose it.
        if streak % threshold:
            return None
        failed = [row for row in rows if _failed(row, now=now, stale_after=stale_after)]
        newest = failed[0]
        return Alert(
            kind=AlertKind.CYCLE_FAILURES,
            failures=streak,
            last_error=newest.error
            or (
                "the process died mid-cycle — the row is still RUNNING"
                if newest.status == "RUNNING"
                else None
            ),
            since=failed[streak - 1].started_at,
        )

    if streak:
        return None

    # Recovery: the newest decided cycle succeeded, and the run behind it was long
    # enough to have been announced. Fires exactly once — the cycle after this one
    # sees an OK in front of the streak and computes 0.
    decided = [
        row
        for row in rows
        if row.status != "RUNNING" or _failed(row, now=now, stale_after=stale_after)
    ]
    if not decided:
        return None
    # streak == 0, so the newest *decided* cycle is an OK one by construction.
    previous = consecutive_failures(decided[1:], now=now, stale_after=stale_after)
    if previous < threshold:
        return None
    return Alert(kind=AlertKind.CYCLE_RECOVERED, failures=previous, since=decided[0].started_at)


@dataclass(frozen=True)
class BarrenOutcome:
    """The four things a ``cycles`` row says about whether it ingested anything.

    ``skip_reasons`` is the ``SkipReason`` vocabulary M8.2 put in the ``skipped`` JSONB
    column, which is what makes this decidable without a migration: "the market was
    shut" and "we chose not to look" already have their own keys and are already stored.
    """

    status: str
    started_at: datetime
    symbols_requested: int
    symbols_scanned: int
    skip_reasons: frozenset[str] = frozenset()


#: Skips that mean **we did not look**, so a cycle carrying one is not barren. Forex is
#: shut ~49 hours a week and deliberately unscanned outside ``forex.scan_hours_utc``; an
#: alert that could not tell those from an outage would fire every Saturday and be muted
#: before the first real one arrived.
NOT_BARREN_REASONS = frozenset({"MARKET_CLOSED", "OUTSIDE_SCAN_HOURS"})


def _barren(outcome: BarrenOutcome) -> bool:
    return (
        outcome.status == "OK"
        and outcome.symbols_requested > 0
        and outcome.symbols_scanned == 0
        and not (outcome.skip_reasons & NOT_BARREN_REASONS)
    )


def consecutive_barren(rows: Sequence[BarrenOutcome]) -> int:
    """How many cycles in a row looked and got nothing, newest first.

    Same shape as :func:`consecutive_failures` and the same treatment of an in-flight
    row: a ``RUNNING`` cycle is neither, and must not reset a count that the next cycle
    is about to complete. A ``FAILED`` cycle *does* break the streak — it is a different
    condition with a different alert, and reporting one outage twice trains the reader
    to ignore both.
    """
    streak = 0
    for outcome in rows:
        if _barren(outcome):
            streak += 1
        elif outcome.status == "RUNNING":
            continue
        else:
            break
    return streak


def barren_alert(
    rows: Sequence[BarrenOutcome],
    *,
    market: str,
    threshold: int = DEFAULT_THRESHOLD,
) -> Alert | None:
    """The alert a run of empty cycles justifies, or ``None`` — M10e step 4.

    **Why this exists.** Forex ran for a full trading day ingesting nothing: every pair
    skipped every cycle on a staleness rail that could not pass on a Monday (defect
    #31). Every one of those cycles logged ``status: OK``, ``spend_usd: 0``, and left
    ``/health`` green, because none of them *failed* — they succeeded at producing
    nothing. The existing rails all watch for something going wrong; this one watches
    for nothing going right, which is the failure mode HANDOFF §4 item 1 names and the
    only one this system has repeatedly been unable to see.

    **Why the threshold is the same 3.** ARCHITECTURE.md §2 and §6 already spend that
    number on consecutive cycle failures, and a second, adjacent rail with a second,
    different constant is a number somebody will misremember. Three cycles is three
    hours at either market's hourly cadence, so a Monday-morning outage is on the phone
    by 10:00Z rather than never, and three of forex's ~13 daily cycles is far enough
    from one that a single Saxo 5xx or a token refresh spanning two polls cannot reach
    it — the credential chain has a one-hour memory by design (§3.1).

    Unlike :func:`cycle_alert` this does **not** re-alert on multiples of the threshold.
    The condition it reports persists for the whole of the day it starts on, and four
    identical messages before lunch is the alert fatigue ``calendar_coverage_key`` was
    written to avoid. The caller dedups to one per market per UTC day instead.
    """
    if threshold < 1:
        raise ValueError("barren threshold must be at least 1 cycle")
    streak = consecutive_barren(rows)
    if streak != threshold:
        return None
    barren = [row for row in rows if _barren(row)]
    return Alert(
        kind=AlertKind.MARKET_BARREN,
        failures=streak,
        since=barren[streak - 1].started_at,
        detail=market,
    )


def reauth_alert(*, authorize_url: str, detail: str, since: datetime | None = None) -> Alert:
    """The forex re-authentication alert (specs/FOREX.md §3.1).

    Saxo's access token lives ~20 minutes and its refresh token ~1 hour, and the
    refresh token rotates on every use. Together that gives the credential chain a
    **one-hour memory**: routine operation is fine, a deploy or a restart inside the
    hour survives, and any outage longer than that kills the chain outright. There is
    no unattended credential at this account tier — Certificate Based Authentication
    is the only server-to-server option Saxo documents and it is partners-only — so
    this is a standing operational cost of the provider, not a bug to fix.

    Which is exactly why the alert must **name re-authentication as the action and
    carry the URL**. The owner reading it has a two-minute browser login to do; a
    message saying only "forex paused" would send them hunting a data problem.

    ``authorize_url`` is a configured endpoint, never a value read from a response.
    """
    return Alert(
        kind=AlertKind.FOREX_REAUTH_REQUIRED,
        detail=detail,
        since=since,
        last_error=authorize_url,
    )


def calendar_alert(
    *, usable: bool, detail: str, actions: Sequence[str], coverage_until: str | None
) -> Alert:
    """The economic-calendar coverage alert (specs/FOREX.md §8's staleness rail).

    **The rail was built at M10b and never wired to anything.** ``health()`` was called
    only from inside ``blackout()``, so the ``warn_within_days`` branch composed a
    sentence that nobody ever read: the calendar could run out, forex would fall
    silent, and the first anybody would know is that no forex card had arrived for a
    while. Silence reading as a quiet market is the exact failure HANDOFF §4 item 1 is
    about, which is why the test for this asserts a message in an outbox rather than a
    return value.

    Two states, one alert, and the wording distinguishes them because the consequences
    differ: **expiring** is a warning with signals still flowing, **expired** means
    forex is emitting nothing at all until the file is extended.
    """
    return Alert(
        kind=AlertKind.FOREX_CALENDAR_COVERAGE,
        detail=detail,
        last_error=coverage_until,
        actions=tuple(actions),
        failures=0 if usable else 1,
    )


__all__ = [
    "DEFAULT_THRESHOLD",
    "NOT_BARREN_REASONS",
    "Alert",
    "AlertKind",
    "BarrenOutcome",
    "CycleOutcome",
    "barren_alert",
    "calendar_alert",
    "consecutive_barren",
    "consecutive_failures",
    "cycle_alert",
    "reauth_alert",
]
