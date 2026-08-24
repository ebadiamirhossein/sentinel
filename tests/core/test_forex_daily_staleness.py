"""The daily-staleness rail — FOREX.md defect #31, M10e.

**Why this file exists.** M10d switched forex on at 11:29 on Saturday 2026-08-22. The
first trading day a forex cycle ever ran on was Monday 2026-08-24, and on that day every
cycle skipped every pair with ``stale candles: 1d``, reported ``status: OK``, spent
$0.00 and raised nothing. The rule measured wall-clock age against a 48-hour budget; on
a Monday the newest *closed* daily bar is Friday's, 79 hours old at 07:00Z and 90 at
18:00Z. It could not have passed.

**Nothing caught it because every forex test in this repo runs on a Wednesday.**
``saxo_double.NOW`` is 2026-08-12 12:05Z. The double's daily grid was always right — it
emits 00:00Z labels on weekdays, exactly as D-k measured Saxo's — so no fixture was
wrong and no mock was lying. The untested axis was the day of the week. Point the same
double at a Monday and the live defect reproduces on the first try.

So these tests move along that axis and only that axis: the same venue, the same
symbols, the same assembler, on the days nobody had asked it about. Each one that must
pass has a sibling that must fail, because a recency rail that cannot fire is worse than
no rail at all (FOREX.md #21, #30).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import structlog

from sentinel.core.config import AppConfig, load_config
from sentinel.core.forex_cycle import ForexAssembly, assemble_forex
from sentinel.fx.hours import daily_freshness, observed_hours_since
from tests.core.saxo_double import SyntheticSaxo, build

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY"]

#: The two ends of the scan window. ``forex.scan_hours_utc`` is ``[7, 19]`` and the
#: orchestrator tests ``start <= hour < end``, so 18:00Z is the last cycle of the day —
#: and the one where the daily bar is oldest and the margin thinnest.
FIRST_HOUR, LAST_HOUR = 7, 18

#: Summer: 17:00 America/New_York is 21:00Z. Winter: 22:00Z. Both are Mondays, and the
#: pair is the whole DST argument — the anchor moves an hour between them and the rule
#: below has no term that depends on it.
SUMMER_MONDAY = date(2026, 8, 24)
WINTER_MONDAY = date(2027, 1, 25)


async def run(transport: SyntheticSaxo, *, config: AppConfig | None = None) -> ForexAssembly:
    adapter, client = build(transport)
    async with client:
        return await assemble_forex(
            adapter, SYMBOLS, config=config or load_config(), now=transport.now
        )


def at(on: date, hour: int) -> datetime:
    return datetime(on.year, on.month, on.day, hour, tzinfo=UTC)


# ── the Monday that emptied the market ──────────────────────────────────────


@pytest.mark.parametrize("hour", [FIRST_HOUR, LAST_HOUR])
@pytest.mark.parametrize("monday", [SUMMER_MONDAY, WINTER_MONDAY], ids=["summer", "winter"])
async def test_a_monday_after_a_normal_weekend_is_not_stale(monday: date, hour: int) -> None:
    """The defect, in both seasons. Fails on main with ``stale candles: 1d``.

    The daily bar really is three days old on a Monday morning; that is what a weekend
    is, not what a fault is. Asserted at both ends of the scan window because the margin
    shrinks across the day and 18:00Z is where it is thinnest.
    """
    assembly = await run(SyntheticSaxo(now=at(monday, hour)))
    assert assembly.skipped == {}
    assert [snapshot.symbol for snapshot in assembly.snapshots] == SYMBOLS


@pytest.mark.parametrize("hour", [FIRST_HOUR, LAST_HOUR])
async def test_the_monday_after_a_friday_holiday_is_not_stale(hour: int) -> None:
    """Christmas Day 2026 is a Friday, and so is New Year's Day 2027.

    This is the case that decides the implementation. Subtracting only *weekends* from
    the elapsed time — the obvious fix, and a correct one for the bug as reported —
    leaves the Monday after a closed Friday at 79 hours against a 48-hour budget: the
    identical outage, three times a year. Counting the hours the venue actually served
    costs nothing on a holiday, because a closed day contributes none.
    """
    christmas = date(2026, 12, 25)
    assembly = await run(
        SyntheticSaxo(now=at(date(2026, 12, 28), hour), closed_dates=frozenset({christmas}))
    )
    assert assembly.skipped == {}
    assert len(assembly.snapshots) == len(SYMBOLS)


# ── and the siblings, so the rail can still fail ────────────────────────────


@pytest.mark.parametrize(
    ("monday", "frozen_at"),
    [(SUMMER_MONDAY, date(2026, 8, 21)), (WINTER_MONDAY, date(2027, 1, 22))],
    ids=["summer", "winter"],
)
async def test_a_daily_feed_frozen_at_friday_is_stale_by_tuesday(
    monday: date, frozen_at: date
) -> None:
    """The teeth. Same tail that is legitimately fresh on Monday, one day later.

    On the Monday the newest daily bar being Friday's is the weekend. On the Tuesday it
    is a feed that has stopped, and 55 observed hours clears the 48-hour budget. The
    intraday tails keep running throughout, which is what makes the two distinguishable
    at all — and what makes this a rail rather than a formality.
    """
    tuesday = monday + timedelta(days=1)
    assembly = await run(SyntheticSaxo(now=at(tuesday, FIRST_HOUR), newest_daily_bar=frozen_at))
    assert assembly.snapshots == []
    assert set(assembly.skipped.values()) == {"stale candles: 1d"}


async def test_a_daily_feed_frozen_two_trading_days_is_stale_midweek() -> None:
    """No weekend anywhere in the span, so observed hours and wall clock agree.

    The rail has to fire on the ordinary case too, or the fix has only moved the blind
    spot from Monday to every day.
    """
    assembly = await run(
        SyntheticSaxo(now=at(date(2026, 8, 20), 12), newest_daily_bar=date(2026, 8, 17))
    )
    assert assembly.snapshots == []
    assert set(assembly.skipped.values()) == {"stale candles: 1d"}


# ── the margin, which is the number worth watching (M10e R1) ────────────────


async def test_the_margin_is_logged_on_every_daily_check() -> None:
    """A number worth watching that is not logged is a number nobody watches.

    The Monday margin is single-digit hours against a D-d measurement noise of +/-2, so
    the arithmetic is emitted every cycle rather than recomputed by hand from a report.
    ``wall_clock_hours`` rides along deliberately: it is what the pre-M10e rule would
    have said, so one line carries both the verdict and the verdict it replaced — and
    the gap between them is the only evidence that distinguishes "the fix worked" from
    "it was a Tuesday".
    """
    with structlog.testing.capture_logs() as logs:
        await run(SyntheticSaxo(now=at(SUMMER_MONDAY, LAST_HOUR)))

    lines = [entry for entry in logs if entry["event"] == "forex.daily_freshness"]
    assert [entry["symbol"] for entry in lines] == SYMBOLS
    logged = lines[0]
    assert logged["budget_hours"] == 48
    assert logged["margin_hours"] == logged["budget_hours"] - logged["observed_hours"]
    assert logged["hourly_covers_label"] is True
    # A weekend was subtracted: ~90 hours of clock became ~41 of market.
    assert logged["wall_clock_hours"] - logged["observed_hours"] > 24
    assert logged["margin_hours"] > 0


async def test_midweek_the_two_rulers_agree_within_one_bar() -> None:
    """The other half of R1's discriminator, and a guard on the new rule's units.

    With no closure in the span the observed count must track wall clock, lagging by at
    most the one forming bar the adapter drops. If these two ever diverge mid-week the
    hour counting has a bug, and the Monday result would be luck.
    """
    now = at(date(2026, 8, 19), LAST_HOUR)
    adapter, client = build(SyntheticSaxo(now=now))
    async with client:
        tails = {
            spec.timeframe: await adapter.fetch_tail("EURUSD", spec.timeframe, spec.candles)
            for spec in load_config().forex.timeframes
        }
    freshness = daily_freshness(
        tails["1d"].bid.candles[-1].open_time,
        now=now,
        hourly_open_times=tuple(c.open_time for c in tails["1h"].bid.candles),
        config=load_config().forex,
    )
    assert freshness.wall_clock_hours - freshness.observed_hours <= 1
    assert not freshness.is_stale


async def test_an_hourly_tail_that_cannot_reach_the_label_is_stale() -> None:
    """No denominator is not a pass.

    If the 1h series does not reach back to the daily label there is nothing to count
    the span in, and the honest answer is that the daily bar is older than the whole
    hourly tail — 1200 bars, some fifty days. Reporting fresh would be a rail that opens
    itself whenever its own input is missing.
    """
    label = datetime(2026, 8, 21, tzinfo=UTC)
    freshness = daily_freshness(
        label,
        now=at(SUMMER_MONDAY, FIRST_HOUR),
        hourly_open_times=(),
        config=load_config().forex,
    )
    assert not freshness.covers_label
    assert freshness.is_stale
    assert observed_hours_since(label, at(SUMMER_MONDAY, FIRST_HOUR), hourly_open_times=()) == 0
