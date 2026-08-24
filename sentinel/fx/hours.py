"""The trading week, and why it is checked *before* staleness (FOREX.md §5).

Pure: no clock of its own, no I/O, no config beyond what is handed in.

**Closed is a state, not a fault.** Crypto never closes, so every data rule in this
system was written by somebody who did not have to think about it. Forex closes for
49 hours a week, and a rule that cannot tell "shut" from "broken" will spend every
weekend reporting a fault that is not there — or, worse, spend every weekend
reporting health that is not there either.

**Spec defect #14, recorded 2026-08-21.** FOREX.md §5.1 says crypto's staleness rule
"would fire on every weekend snapshot" and must therefore be preceded by a market-
hours check. The premise is wrong: ``sentinel/ingestion/staleness.py`` compares
``OHLCVSeries.fetched_at``, which is always ~now for a fresh fetch, not the newest
candle's ``open_time``. So the existing rule would not misfire over a weekend — it
would do something worse and report a series whose newest bar is 50 hours old as
perfectly fresh, because the *fetch* was recent.

The conclusion §5.1 reaches is right and its reasoning is inverted. Forex needs a
**candle-recency** check that crypto never needed, and that check is exactly the one
that must be skipped while the market is closed. Both halves live here.

**Spec defect #31, recorded 2026-08-24 (M10e).** Skipping the check while the market is
closed is not enough, because the *weekend does not end when the market reopens*. A
daily bar read on a Monday is Friday's, and 79 hours of wall clock separate them however
the venue stamps it. So the daily rail measures in **observed market hours** — see
:func:`daily_freshness` — while the intraday rails, which are dense and on the UTC grid,
keep the wall-clock rule above unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from sentinel.core.config import ForexConfig
from sentinel.fx.models import ForexRejection
from sentinel.ingestion.staleness import timeframe_to_timedelta

#: ``datetime.weekday()`` values, named so the week rule reads as prose.
MONDAY, FRIDAY, SATURDAY, SUNDAY = 0, 4, 5, 6


class MarketState(StrEnum):
    """Where the clock stands. ``CLOSED`` is normal, and is never DEGRADED."""

    OPEN = "OPEN"
    #: Open, but inside the 19:00-21:00 UTC band where spreads are measurably wide.
    #: A distinct state rather than a flag, because the cost of trading through it is
    #: an order of magnitude different: GBPUSD's 21:00 median spread is 12.0 pips,
    #: which is 0.667R on an 18-pip stop.
    ROLLOVER = "ROLLOVER"
    CLOSED = "CLOSED"

    @property
    def is_open(self) -> bool:
        return self is not MarketState.CLOSED


@dataclass(frozen=True)
class WeekBounds:
    """When the week actually opened, and how far that is from what config expects.

    Both are kept. The observed value is the authority — §5.2 says derive the week
    from candle availability — and the nominal one is what makes a silent DST shift
    visible instead of merely absorbed.
    """

    observed_open_at: datetime
    nominal_open_at: datetime

    @property
    def divergence(self) -> timedelta:
        return self.observed_open_at - self.nominal_open_at

    @property
    def diverged(self) -> bool:
        """Any difference at all. The expected magnitude is exactly one hour, twice a
        year, when the US and EU change daylight saving on different dates."""
        return self.divergence != timedelta(0)


def state_at(now: datetime, config: ForexConfig) -> MarketState:
    """Open, closed, or open-but-in-rollover, from the nominal week (§5.2).

    Nominal because this answers "should there be a market right now"; whether there
    *is* one is answered by candles, and :func:`derive_week_open` reconciles the two.
    """
    if not _inside_the_week(now, config):
        return MarketState.CLOSED
    start = config.rollover_window_start_hour_utc
    end = config.rollover_window_end_hour_utc
    if start <= now.hour < end:
        return MarketState.ROLLOVER
    return MarketState.OPEN


def _inside_the_week(now: datetime, config: ForexConfig) -> bool:
    weekday = now.weekday()
    if weekday == SATURDAY:
        return False
    if weekday == SUNDAY:
        return now.hour >= config.week_open_hour_utc
    if weekday == FRIDAY:
        return now.hour < config.week_close_hour_utc
    return True


def nominal_week_open(now: datetime, config: ForexConfig) -> datetime:
    """The start of the trading week ``now`` belongs to, per config.

    For an instant inside the weekend this returns the open that is *coming*, which
    is what a "how long until the market returns" question wants; for an instant
    inside the week it returns the open that has already happened, which is what the
    week-open quiet period measures from.
    """
    anchor = now.replace(hour=config.week_open_hour_utc, minute=0, second=0, microsecond=0)
    days_since_sunday = (now.weekday() - SUNDAY) % 7
    candidate = anchor - timedelta(days=days_since_sunday)
    if candidate > now and _inside_the_week(now, config):
        candidate -= timedelta(days=7)
    return candidate


def derive_week_open(
    open_times: Sequence[datetime],
    *,
    now: datetime,
    config: ForexConfig,
    min_gap_hours: int = 12,
) -> WeekBounds | None:
    """The week's real open, read from where the candles resume (§5.2).

    Measured and confirmed in journal/M10b_SPIKE.md §6: closed hours are cleanly
    **absent** from a chart response — not zero-filled, not a repeated Friday bar,
    not an error. So the weekend is a hole in the series, and the first bar after the
    hole is the open.

    ``None`` when the tail contains no such hole, which simply means it does not
    reach back to a weekend. That is not a failure and the caller falls back to the
    nominal open.

    **The ambiguity is not resolved here, and deliberately so.** D-d found the same
    Sunday hour present or absent depending on the request's anchor and ``Count``,
    leaving the week open either 19:00Z or 21:00Z. The adapter answers that by never
    specifying an anchor at all, so one framing is used consistently; this function
    reports what that framing showed. ``week_open_quiet_hours`` is what covers the
    remaining doubt, by declining to trade through it.
    """
    ordered = sorted(open_times)
    threshold = timedelta(hours=min_gap_hours)
    observed: datetime | None = None
    for previous, following in pairwise(ordered):
        if following - previous >= threshold:
            observed = following
    if observed is None:
        return None
    return WeekBounds(
        observed_open_at=observed, nominal_open_at=nominal_week_open(observed, config)
    )


@dataclass(frozen=True)
class ClockVerdict:
    """May a signal be emitted at this instant, and if not, under which code."""

    state: MarketState
    reason: ForexRejection | None = None

    @property
    def allowed(self) -> bool:
        return self.reason is None


def clock_verdict(
    now: datetime, *, config: ForexConfig, week_open: datetime | None = None
) -> ClockVerdict:
    """§5's whole clock rail, in the order the reasons matter.

    ``week_open`` is the **observed** open when one could be derived; without it the
    nominal one is used, which is the honest fallback rather than a guess — a tail
    that does not reach a weekend has told us nothing to prefer over config.
    """
    state = state_at(now, config)
    if state is MarketState.CLOSED:
        return ClockVerdict(state, ForexRejection.MARKET_CLOSED)

    opened_at = week_open if week_open is not None else nominal_week_open(now, config)
    if now < opened_at + timedelta(hours=config.week_open_quiet_hours):
        return ClockVerdict(state, ForexRejection.WEEK_OPEN_QUIET)

    if now.weekday() == FRIDAY and now.hour >= config.friday_signal_cutoff_hour_utc:
        return ClockVerdict(state, ForexRejection.FRIDAY_CUTOFF)

    return ClockVerdict(state)


def candles_are_stale(
    newest_open_time: datetime,
    *,
    timeframe: str,
    now: datetime,
    config: ForexConfig,
) -> bool:
    """Is the newest closed bar too old — **asked only while the market is open**?

    Spec defect #14. The caller must check :func:`state_at` first; calling this
    during the weekend would report every Sunday snapshot as stale, which is the
    false-failure §5.1 was trying to prevent by a route it had mis-diagnosed.

    The budget is the same shape crypto uses for its own staleness — a multiple of
    the timeframe — but measured from the **candle**, not from the fetch. That is the
    whole difference, and it is the difference that matters: a fetch is always recent.

    **Intraday only from M10e.** 15m, 1h and 4h sit on the UTC grid in every season
    (D-k) and are dense while the market is open, so wall-clock age is the right
    question for them and this rule is unchanged. The 1d bar is a different animal and
    has :func:`daily_freshness`; see defect #31 for why asking it this question emptied
    forex for a day.
    """
    budget = timeframe_to_timedelta(timeframe) * config.max_candle_age_multiplier
    return (now - newest_open_time) > budget


@dataclass(frozen=True)
class DailyFreshness:
    """The 1d rail's whole arithmetic, kept rather than reduced to a bool.

    The verdict is one number away from the margin, and the margin is the number worth
    watching (M10e R1): the rail clears a Monday evening by 4-6 observed hours against
    a D-d measurement noise of +/-2. A margin that thin has to be visible in the logs,
    so this carries every term the decision used, including ``wall_clock_hours`` — what
    the pre-M10e rule would have computed, so one log line shows both the verdict and
    the verdict it replaced.
    """

    newest_label: datetime
    observed_hours: int
    budget_hours: int
    wall_clock_hours: int
    #: Does the hourly tail reach back to the daily label? When it does not, the count
    #: below is a floor rather than a measurement and the bar is stale by construction.
    covers_label: bool

    @property
    def margin_hours(self) -> int:
        return self.budget_hours - self.observed_hours

    @property
    def is_stale(self) -> bool:
        return not self.covers_label or self.observed_hours > self.budget_hours


def observed_hours_since(
    label: datetime, now: datetime, *, hourly_open_times: Sequence[datetime]
) -> int:
    """How many hourly bars the venue itself placed in ``[label, now)``.

    The market-hours analogue of ``now - label``: identical when nothing was shut in
    between, and smaller by exactly the closure when something was.
    """
    return sum(1 for at in hourly_open_times if label <= at < now)


def daily_freshness(
    newest_label: datetime,
    *,
    now: datetime,
    hourly_open_times: Sequence[datetime],
    config: ForexConfig,
) -> DailyFreshness:
    """The 1d recency rule, measured in **observed market hours** — defect #31.

    Wall clock is the wrong ruler for a daily bar. The forex market is shut ~49 hours a
    week and the scan window is 07:00-19:00Z, so on any Monday the newest *closed* daily
    bar is Friday's: 79 hours old at 07:00Z and 90 at 18:00Z, against a 48-hour budget.
    Every symbol was skipped, every Monday, and M10d's switch-on landed on a Saturday so
    the first trading day forex ever saw was one of them.

    **The New York anchor is not the cause and correcting for it does not help.** D-k
    measured the 1d ``Time`` as a date label: a bar stamped ``D 00:00Z`` really spans
    ``D-1 21:00Z -> D 21:00Z`` in summer. The label is therefore three hours *newer*
    than the bar's real start, so anchoring the arithmetic makes the age worse (82.6h ->
    85.6h at the cycle that was logged) and changes no verdict at any hour of any day.

    **The hourly tail is the venue's own trading calendar.** Closed hours are cleanly
    absent from a Saxo chart response (journal/M10b_SPIKE.md §6), so weekends, market
    holidays and the 17:00 NY anchor are all already recorded in the 1h series as holes.
    Counting them has no timezone term to get wrong twice a year — DST correctness is
    inherited from the data rather than derived — and it costs nothing on a holiday,
    which is what rules out the obvious clock-derived alternative: Christmas Day 2026
    and New Year's Day 2027 are both Fridays, and subtracting only weekends reproduces
    this exact outage on the Monday after each.

    The budget is unchanged. ``max_candle_age_multiplier`` days of *observed* hours is
    still "you may be one daily bar behind, no more", and a daily feed frozen at Friday
    is still stale by Tuesday morning (55 observed hours against 48).
    """
    budget = timeframe_to_timedelta("1d") * config.max_candle_age_multiplier
    return DailyFreshness(
        newest_label=newest_label,
        observed_hours=observed_hours_since(newest_label, now, hourly_open_times=hourly_open_times),
        budget_hours=round(budget.total_seconds() / 3600),
        wall_clock_hours=round((now - newest_label).total_seconds() / 3600),
        covers_label=bool(hourly_open_times) and min(hourly_open_times) <= newest_label,
    )


__all__ = [
    "FRIDAY",
    "MONDAY",
    "SATURDAY",
    "SUNDAY",
    "ClockVerdict",
    "DailyFreshness",
    "MarketState",
    "WeekBounds",
    "candles_are_stale",
    "clock_verdict",
    "daily_freshness",
    "derive_week_open",
    "nominal_week_open",
    "observed_hours_since",
    "state_at",
]
