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
    """
    budget = timeframe_to_timedelta(timeframe) * config.max_candle_age_multiplier
    return (now - newest_open_time) > budget


__all__ = [
    "FRIDAY",
    "MONDAY",
    "SATURDAY",
    "SUNDAY",
    "ClockVerdict",
    "MarketState",
    "WeekBounds",
    "candles_are_stale",
    "clock_verdict",
    "derive_week_open",
    "nominal_week_open",
    "state_at",
]
