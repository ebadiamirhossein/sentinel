"""The trading week — docs/specs/FOREX.md §5, and spec defect #14.

The measured facts these tests encode come from journal/M10b_SPIKE.md §6: closed
hours are cleanly **absent** from a chart response (not zero-filled, not a repeated
Friday bar, not an error), and the last Friday bar is stamped 20:00Z covering
20:00-21:00, so the week closes at 21:00 UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sentinel.core.config import ForexConfig
from sentinel.fx.hours import (
    FRIDAY,
    SATURDAY,
    SUNDAY,
    MarketState,
    candles_are_stale,
    clock_verdict,
    daily_freshness,
    derive_week_open,
    nominal_week_open,
    observed_hours_since,
    state_at,
)
from sentinel.fx.models import ForexRejection

CONFIG = ForexConfig()
NEW_YORK = ZoneInfo("America/New_York")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """August 2026: the 14th is a Friday, the 15th a Saturday, the 16th a Sunday."""
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


# ── open, closed, rollover ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "hour", "expected"),
    [
        (14, 18, MarketState.OPEN),  # Friday afternoon
        (14, 19, MarketState.ROLLOVER),  # Friday, rollover band opens
        (14, 20, MarketState.ROLLOVER),  # the last Friday bar
        (14, 21, MarketState.CLOSED),  # the week has closed
        (14, 23, MarketState.CLOSED),
        (15, 12, MarketState.CLOSED),  # Saturday, all day
        (16, 20, MarketState.CLOSED),  # Sunday, before the open
        (16, 21, MarketState.ROLLOVER),  # Sunday open, inside the 19-22 band
        (16, 22, MarketState.OPEN),
        (17, 9, MarketState.OPEN),  # Monday, London
        (19, 21, MarketState.ROLLOVER),  # Wednesday rollover (triple swap)
        (19, 22, MarketState.OPEN),  # and back to normal by 22:00
    ],
)
def test_the_week_and_the_rollover_band(day: int, hour: int, expected: MarketState) -> None:
    assert state_at(at(day, hour), CONFIG) is expected


def test_the_rollover_band_covers_the_three_bars_the_spike_measured() -> None:
    """§3 of the findings: elevated spreads at the bars stamped 19, 20 and 21, and
    normal ones again at 22. An off-by-one hour here would leave the worst bar out."""
    elevated = {h for h in range(24) if state_at(at(17, h), CONFIG) is MarketState.ROLLOVER}
    assert elevated == {19, 20, 21}


def test_closed_is_a_state_and_never_a_degraded_read() -> None:
    """§5.1. A weekend is not a fault, and the code that says so is its own."""
    verdict = clock_verdict(at(15, 12), config=CONFIG)
    assert verdict.state is MarketState.CLOSED
    assert verdict.reason is ForexRejection.MARKET_CLOSED
    assert verdict.allowed is False


# ── the quiet periods ───────────────────────────────────────────────────────


def test_no_signals_in_the_first_hours_of_the_week() -> None:
    """§5.2. D-d leaves the Sunday open ambiguous between 19:00Z and 21:00Z, and
    Sunday-evening liquidity is thin regardless, so nothing is lost by waiting."""
    opened = at(16, 21)
    assert clock_verdict(opened, config=CONFIG).reason is ForexRejection.WEEK_OPEN_QUIET
    assert (
        clock_verdict(opened + timedelta(hours=2), config=CONFIG).reason
        is ForexRejection.WEEK_OPEN_QUIET
    )
    assert clock_verdict(opened + timedelta(hours=3), config=CONFIG).allowed


def test_the_quiet_period_is_measured_from_the_observed_open_not_the_nominal_one() -> None:
    """If the candles say the week opened at 19:00Z, the quiet period starts there."""
    observed = at(16, 19)
    assert clock_verdict(at(16, 22), config=CONFIG, week_open=observed).allowed
    assert clock_verdict(at(16, 22), config=CONFIG).reason is ForexRejection.WEEK_OPEN_QUIET, (
        "with no observed open, the nominal 21:00Z one keeps us quiet an hour longer"
    )


def test_no_new_signals_after_the_friday_cutoff() -> None:
    """§5.4. A ladder placed now cannot fill before the weekend, and one that did
    would carry gap risk through it."""
    assert clock_verdict(at(14, 18), config=CONFIG).allowed
    assert clock_verdict(at(14, 19), config=CONFIG).reason is ForexRejection.FRIDAY_CUTOFF
    assert clock_verdict(at(14, 20, 59), config=CONFIG).reason is ForexRejection.FRIDAY_CUTOFF
    # Past the close it is simply shut, which is the wider statement.
    assert clock_verdict(at(14, 21), config=CONFIG).reason is ForexRejection.MARKET_CLOSED


# ── deriving the week from the candles ──────────────────────────────────────


def weekend_shaped_tail(*, open_at: datetime) -> list[datetime]:
    """Hourly stamps that stop at Friday 20:00Z and resume at ``open_at`` (§6)."""
    before = [at(14, hour) for hour in range(8, 21)]
    after = [open_at + timedelta(hours=index) for index in range(12)]
    return before + after


@pytest.mark.parametrize("open_hour", [19, 21], ids=["sunday-19Z", "sunday-21Z"])
def test_the_week_open_is_read_from_where_the_candles_resume(open_hour: int) -> None:
    """§5.2. The weekend is a hole in the series and the first bar after it is the
    open — both of D-d's two answers are reported faithfully, not normalised away."""
    opened = at(16, open_hour)
    bounds = derive_week_open(weekend_shaped_tail(open_at=opened), now=at(17, 9), config=CONFIG)
    assert bounds is not None
    assert bounds.observed_open_at == opened
    assert bounds.nominal_open_at == at(16, 21)
    assert bounds.diverged is (open_hour != 21)


def test_a_dst_shifted_open_is_reported_rather_than_absorbed() -> None:
    """The boundary moves by an hour twice a year because the US and EU change
    daylight saving on different dates. That must be visible, not silently accepted."""
    opened = at(16, 22)
    bounds = derive_week_open(weekend_shaped_tail(open_at=opened), now=at(17, 9), config=CONFIG)
    assert bounds is not None
    assert bounds.diverged
    assert bounds.divergence == timedelta(hours=1)


def test_a_tail_with_no_weekend_in_it_yields_nothing_rather_than_a_guess() -> None:
    inside_one_week = [at(17, hour) for hour in range(0, 24)]
    assert derive_week_open(inside_one_week, now=at(17, 23), config=CONFIG) is None


def test_the_nominal_open_looks_backwards_inside_the_week_and_forwards_outside_it() -> None:
    assert nominal_week_open(at(19, 12), CONFIG) == at(16, 21)  # Wednesday -> last Sunday
    assert nominal_week_open(at(16, 12), CONFIG) == at(16, 21)  # Sunday noon -> tonight


# ── candle recency (spec defect #14) ────────────────────────────────────────


def test_a_stale_candle_is_caught_while_the_market_is_open() -> None:
    """The check crypto never needed, because crypto never closes."""
    now = at(17, 9)
    assert not candles_are_stale(at(17, 8), timeframe="1h", now=now, config=CONFIG)
    assert not candles_are_stale(at(17, 7), timeframe="1h", now=now, config=CONFIG)
    assert candles_are_stale(at(17, 6, 59), timeframe="1h", now=now, config=CONFIG)


def test_the_recency_check_is_measured_from_the_candle_not_from_the_fetch() -> None:
    """Spec defect #14, stated as an assertion.

    §5.1 expected crypto's staleness rule to misfire every weekend. It compares
    ``OHLCVSeries.fetched_at``, which is always ~now, so it would have done the
    opposite: reported a series whose newest bar is fifty hours old as fresh. This
    check reads the bar, which is why it catches the case at all — and why the caller
    must consult ``state_at`` first so it is not asked during the weekend, when a bar
    fifty hours old is simply correct.
    """
    friday_close = at(14, 20)
    sunday_midday = at(16, 12)
    assert state_at(sunday_midday, CONFIG) is MarketState.CLOSED
    # If it *were* asked while closed, it would say stale -- which is precisely why
    # market hours are checked first (§5.1).
    assert candles_are_stale(friday_close, timeframe="1h", now=sunday_midday, config=CONFIG)


def test_a_wider_timeframe_gets_a_wider_budget() -> None:
    now = at(17, 12)
    assert not candles_are_stale(at(17, 4), timeframe="4h", now=now, config=CONFIG)
    assert candles_are_stale(at(17, 3), timeframe="4h", now=now, config=CONFIG)


# ── the daily bar's own rule (defect #31, M10e) ─────────────────────────────


def hourly_grid(start: datetime, end: datetime) -> tuple[datetime, ...]:
    """Every hour the venue serves in ``[start, end)`` — weekends simply absent.

    The week boundary is 17:00 America/New_York, derived per date rather than written
    down as a UTC hour. That matters here and nowhere else in this file: on a changeover
    weekend the Friday close and the Sunday open have *different* UTC hours, so a single
    constant could not express the 47- and 49-hour weekends the tests below rely on.
    """
    out, cursor = [], start
    while cursor < end:
        anchor = (
            datetime(cursor.year, cursor.month, cursor.day, 17, tzinfo=NEW_YORK)
            .astimezone(UTC)
            .hour
        )
        weekday = cursor.weekday()
        open_now = not (
            weekday == SATURDAY
            or (weekday == SUNDAY and cursor.hour < anchor)
            or (weekday == FRIDAY and cursor.hour >= anchor)
        )
        if open_now:
            out.append(cursor)
        cursor += timedelta(hours=1)
    return tuple(out)


def test_observed_hours_ignore_the_hours_the_venue_did_not_serve() -> None:
    """The whole fix in one assertion. 79 hours of clock, 31 hours of market.

    Friday's daily bar read on Monday morning: the wall clock says nearly three and a
    half days and the market says a day and a bit, because the market was shut for the
    two days in between. The second number is the one a recency budget means.
    """
    label = datetime(2026, 8, 21, tzinfo=UTC)  # Friday's 1d bar, stamped 00:00Z (D-k)
    monday = datetime(2026, 8, 24, 7, tzinfo=UTC)
    grid = hourly_grid(label, monday)

    assert (monday - label) == timedelta(hours=79)
    assert observed_hours_since(label, monday, hourly_open_times=grid) == 31


@pytest.mark.parametrize(
    ("friday", "monday", "weekend_hours"),
    [
        # An ordinary summer weekend and an ordinary winter one: 48 hours, an hour apart
        # in UTC and the same instant in New York.
        (datetime(2026, 8, 21, tzinfo=UTC), datetime(2026, 8, 24, 7, tzinfo=UTC), 48),
        (datetime(2027, 1, 22, tzinfo=UTC), datetime(2027, 1, 25, 7, tzinfo=UTC), 48),
        # Spring forward, 2026-03-08: Friday closes at 22:00Z (EST) and Sunday opens at
        # 21:00Z (EDT), so that weekend is 47 hours long.
        (datetime(2026, 3, 6, tzinfo=UTC), datetime(2026, 3, 9, 7, tzinfo=UTC), 47),
        # Fall back, 2026-11-01: Friday closes at 21:00Z (EDT), Sunday opens at 22:00Z
        # (EST). 49 hours.
        (datetime(2026, 10, 30, tzinfo=UTC), datetime(2026, 11, 2, 7, tzinfo=UTC), 49),
    ],
    ids=["summer", "winter", "spring-forward", "fall-back"],
)
def test_the_daily_rule_survives_both_sides_of_the_daylight_saving_boundary(
    friday: datetime, monday: datetime, weekend_hours: int
) -> None:
    """The anchor moves by an hour twice a year and the verdict does not move at all.

    D-k measured Saxo's daily anchor at 21:00Z in August and 22:00Z in January, and the
    two changeover weekends are 47 and 49 hours rather than 48 — the US changes at 02:00
    local, which falls inside the weekend. A rule that *computed* the anchor would have a
    term to get wrong in each of these four rows. This one counts bars, so the season is
    carried by the data and never appears in the arithmetic.

    Note the spring-forward row: a 47-hour weekend leaves the *most* market time between
    the same two calendar instants, and it is still nowhere near the budget.
    """
    grid = hourly_grid(friday, monday)
    freshness = daily_freshness(friday, now=monday, hourly_open_times=grid, config=CONFIG)

    assert freshness.wall_clock_hours == 79
    assert freshness.observed_hours == 79 - weekend_hours
    assert freshness.budget_hours == 48
    assert freshness.covers_label
    assert not freshness.is_stale
    assert freshness.margin_hours == 48 - freshness.observed_hours


def test_the_daily_rule_still_fires_when_the_feed_actually_stops() -> None:
    """One day later, the identical tail is no longer a weekend — it is a fault.

    This is the assertion that keeps the rail a rail. FOREX.md #21 and #30 are both the
    same lesson twice: a threshold widened until the symptom disappears is a checklist
    item, not a control. The budget here is untouched at 48 hours; it is the ruler that
    changed, and the ruler still reaches.
    """
    label = datetime(2026, 8, 21, tzinfo=UTC)
    tuesday = datetime(2026, 8, 25, 7, tzinfo=UTC)
    grid = hourly_grid(label, tuesday)

    freshness = daily_freshness(label, now=tuesday, hourly_open_times=grid, config=CONFIG)
    assert freshness.observed_hours == 55
    assert freshness.is_stale
    assert freshness.margin_hours == -7
