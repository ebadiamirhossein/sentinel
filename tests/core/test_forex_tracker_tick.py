"""Join 2 of journal/M10c_REPORT.md §13: a forex tracker tick over a real Saxo tail.

M10b-2 §2's rule, applied to the boundary M10c left behind: **when a milestone splits
a producer from its consumer, the next one composes them first and builds second.**
Everything below the HTTP layer is real — the real ``SaxoForexAdapter``, the real
``PriceFeed``, the real ``TrackerLoop`` and the real detection path — over
``tests/core/saxo_double.SyntheticSaxo``, which serves the venue's own grids with the
weekend simply absent.

Three things this composition found, and the first would have shipped:

1. **The forex tracker job could not build its adapter at all.** ``app.track_for``
   went through ``wiring.market_adapter``, which supplies neither an HTTP fetcher nor
   an access-token provider — and ``wiring._saxo`` raises ``UnknownAdapter`` without
   both. The exception was swallowed by ``track()``'s ``except Exception`` into a log
   line, so on switch-on day ``tracker:forex`` would have died on every tick for ever
   while ``/health`` stayed green.

2. **``invalidation_candles`` threw away the newest closed 1h bar.** Saxo has no
   closed flag, so the adapter drops the forming bar itself (§4.1); ``PriceFeed`` then
   drops the last row *again*, because on Binance that row is the in-progress candle.
   Two drops, one forming bar: forex measured invalidation an hour late, on a rule
   specs/TELEGRAM_UX.md §4 words as a **close**.

3. **The fill read was two minutes behind on every tick.** Same cause, different cost:
   §4.1's ``now >= T + H + grace`` leaves the newest *closed* 1m bar 120 s old, and M7
   reads 1m HIGH/LOW precisely so a wick is never missed. §4.1's rule exists to keep a
   forming bar out of RSI, ATR and the EMA stack; a high and a low are prices that
   have already traded, so the fill read takes the forming bar and the indicator reads
   do not.

What it **dis**proved is worth recording too, because it was the loudest of the three
predictions: a lookback spanning a weekend does **not** produce a ``DegradedRead``.
``fetch_tail`` sends no ``Mode`` and no ``Time`` (D-d), so the venue returns the newest
``Count`` bars and walks back through the gap — 1198 of a requested 1200 at Monday
08:05 after a Friday 18:00 last tick, the two missing ones being the forming bars.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sentinel.bot.models import SignalRecord
from sentinel.core import app as app_module
from sentinel.core.app import AppState, _schedule_pipeline
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings
from sentinel.core.markets import Market
from sentinel.fx.pricing import ForexCandleSource
from sentinel.tracker.loop import TrackerLoop
from sentinel.tracker.prices import PriceFeed
from tests.bot_double import owner_account
from tests.core.saxo_double import NOW, SyntheticSaxo, build
from tests.fx.forex_double import forex_plan
from tests.tracker_double import FakeDatabase, FakeStore, fake_repositories

OWNER = 111
SYMBOL = "EURUSD"

#: A Monday morning, with the previous tick before the weekend close. The case the
#: ``DegradedRead`` prediction was about.
MONDAY = datetime(2026, 8, 17, 8, 5, tzinfo=UTC)
FRIDAY_LAST_TICK = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)


@pytest.fixture
def settings(repo_config: AppConfig) -> Settings:
    return Settings(secrets=Secrets(_env_file=None), config=repo_config)


def feed(transport: SyntheticSaxo, settings: Settings) -> tuple[PriceFeed, object]:
    """A real ``PriceFeed`` over a real adapter over the synthetic venue."""
    adapter, client = build(transport)
    source = ForexCandleSource(adapter, settings.config.forex, settings.config.tracker)
    return PriceFeed(
        source, settings.config.tracker, max_candles=settings.config.forex.max_count
    ), client


# --------------------------------------------------------------------------- #
# 1 — the fill read: the tick must see the minute it is running in
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_fill_read_reaches_the_current_minute(settings: Settings) -> None:
    """M7 reads 1m HIGH/LOW so a 60-second poll cannot miss the wick that filled a rung.

    Against Saxo's closed-candle rule that guarantee was two minutes stale, which is
    two ticks: ``now >= T + H + grace`` with H=1min and grace=30s leaves the newest
    *closed* 1m bar 120 s old. The regression this pins is not "an error happened" —
    nothing errored — it is that the newest bar was old.
    """
    prices, client = feed(SyntheticSaxo(now=NOW), settings)
    try:
        candles = await prices.fill_candles(SYMBOL, since=NOW - timedelta(seconds=60), now=NOW)
    finally:
        await client.aclose()  # type: ignore[attr-defined]

    assert candles, "the fill read returned nothing at all"
    age = (NOW - candles[-1].open_time).total_seconds()
    assert age < 120, (
        f"the newest 1m bar is {age:.0f}s old — a fill or a stop-out inside that "
        f"window is invisible to this tick, which is the blind spot M7 removed"
    )


@pytest.mark.asyncio
async def test_a_weekend_gap_is_not_a_degraded_read(settings: Settings) -> None:
    """The prediction that failed, kept as a test because it is the expensive case.

    ``fetch_tail`` asserts ``len(rows) == requested`` and calls a short return a
    degraded read (D-e). A Monday tick whose last look was Friday asks for the ceiling,
    and the window it covers is mostly a closed market — so a short return here would
    fail every Monday-morning tick. It does not: no anchor is sent, so the venue
    returns the newest ``Count`` bars and walks back through the gap.
    """
    prices, client = feed(SyntheticSaxo(now=MONDAY), settings)
    try:
        candles = await prices.fill_candles(SYMBOL, since=FRIDAY_LAST_TICK, now=MONDAY)
    finally:
        await client.aclose()  # type: ignore[attr-defined]

    assert len(candles) > 1000, f"expected a full ceiling-sized tail, got {len(candles)}"
    assert candles[-1].open_time <= MONDAY


# --------------------------------------------------------------------------- #
# 2 — the invalidation read: a close is a close
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_invalidation_read_keeps_the_newest_closed_hour(settings: Settings) -> None:
    """specs/TELEGRAM_UX.md §4 measures invalidation on a **close**, and the newest
    close is the one that matters. Dropping the forming bar twice lost it."""
    prices, client = feed(SyntheticSaxo(now=NOW), settings)
    try:
        candles = await prices.invalidation_candles(SYMBOL, limit=3)
    finally:
        await client.aclose()  # type: ignore[attr-defined]

    assert len(candles) == 3, f"asked for 3 closed hours, got {len(candles)}"
    newest = candles[-1].open_time
    assert newest == NOW.replace(hour=11, minute=0, second=0, microsecond=0), (
        f"the newest closed 1h bar at {NOW.isoformat()} is 11:00Z; this read stopped "
        f"at {newest.isoformat()}, an hour behind"
    )


@pytest.mark.asyncio
async def test_the_invalidation_read_never_returns_a_forming_bar(settings: Settings) -> None:
    """The sibling that keeps the fix above from becoming the opposite defect.

    specs/PROMPTS.md §2 rule e and §4.1 both rest on a close being closed. A read that
    solved the missing hour by handing back the *forming* hour would pass the test
    above and invalidate a signal on a bar that has not finished.
    """
    prices, client = feed(SyntheticSaxo(now=NOW), settings)
    try:
        candles = await prices.invalidation_candles(SYMBOL, limit=3)
    finally:
        await client.aclose()  # type: ignore[attr-defined]

    grace = timedelta(seconds=settings.config.forex.candle_grace_seconds)
    for candle in candles:
        assert candle.open_time + timedelta(hours=1) + grace <= NOW, (
            f"the bar stamped {candle.open_time.isoformat()} had not closed at "
            f"{NOW.isoformat()} — §4.1's rule is now >= T + H + grace"
        )


# --------------------------------------------------------------------------- #
# 3 — the whole tick, over the real chain
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_forex_tick_runs_end_to_end_over_a_real_saxo_tail(settings: Settings) -> None:
    """The join itself: an open forex signal, priced from candles the adapter parsed.

    Asserted on the **feed** as well as on the result, because ``checked == 1`` would
    also be true of a tick that fetched nothing and found nothing — the shape
    journal/M8_REPORT.md names silence-as-success.
    """
    store = FakeStore()
    store.users[OWNER] = owner_account(OWNER, capital_eur=Decimal("10000"))
    plan = forex_plan(settings.config)
    store.add_signal(SignalRecord(plan=plan, user_id=OWNER, number=1, market=Market.FOREX))

    transport = SyntheticSaxo(now=NOW)
    prices, client = feed(transport, settings)
    loop = TrackerLoop(
        FakeDatabase(store),  # type: ignore[arg-type]
        prices,
        settings,
        market=Market.FOREX,
        clock=FrozenClock(NOW),
        repositories=fake_repositories(),
    )
    try:
        result = await loop.tick()
    finally:
        await client.aclose()  # type: ignore[attr-defined]

    assert result.failures == [], result.failures
    assert result.closed is False
    assert result.checked == 1
    assert transport.chart_requests, "the tick never reached the venue"
    horizons = {request["Horizon"] for request in transport.chart_requests}
    assert "1" in horizons, f"no 1m fill read was made; horizons seen: {sorted(horizons)}"


# --------------------------------------------------------------------------- #
# 4 — the wiring: the job must be able to build the thing it ticks with
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_forex_tracker_job_builds_a_forex_feed_and_does_not_fail(
    repo_config: AppConfig,
) -> None:
    """The registered ``tracker:forex`` closure, executed rather than counted.

    ``tests/core/test_scan_jobs.py`` asserts which job **ids** are registered, which is
    what let this through: the id was right and the body could not run. So this drives
    the real closure and asserts two things a log line would not distinguish — that the
    tick did not fail, and that what it ticked with was a :class:`ForexCandleSource`
    rather than a raw adapter that answers ``PriceFeed``'s two questions with one wrong
    filter.
    """
    config = repo_config.model_copy(deep=True)
    config.markets[Market.FOREX] = config.markets[Market.FOREX].model_copy(update={"enabled": True})
    settings = Settings(secrets=Secrets(_env_file=None), config=config)

    transport = SyntheticSaxo(now=NOW)
    adapter, client = build(transport)
    seen: dict[str, object] = {}

    @asynccontextmanager
    async def fake_forex_adapter(*_: object, **__: object) -> AsyncIterator[object]:
        yield adapter

    class SpyLoop:
        def __init__(self, _database: object, feed: PriceFeed, *_: object, **__: object) -> None:
            seen["source"] = feed._source

        async def tick(self) -> object:
            return SimpleNamespace(paused_users=())

    scheduler = AsyncIOScheduler(timezone=UTC)
    state = AppState(settings=settings, database=None, scheduler=scheduler)  # type: ignore[arg-type]
    with (
        patch.object(app_module, "forex_adapter", fake_forex_adapter),
        patch.object(app_module, "TrackerLoop", SpyLoop),
    ):
        _schedule_pipeline(scheduler, state, settings, None)  # type: ignore[arg-type]
        job = scheduler.get_job("tracker:forex")
        assert job is not None, "no forex tracker job was registered"
        with structlog.testing.capture_logs() as captured:
            await job.func()

    await client.aclose()

    failures = [event for event in captured if event["event"] == "scheduler.tick_failed"]
    assert failures == [], failures
    assert isinstance(seen.get("source"), ForexCandleSource), (
        f"the forex tracker ticked with {type(seen.get('source')).__name__}, not a "
        f"ForexCandleSource"
    )
