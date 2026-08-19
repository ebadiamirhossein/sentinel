"""One tick end to end, and what a crash in the middle of one costs.

ARCHITECTURE.md §6: "Process crash -> Docker restart; scheduler resumes; tracker
rebuilds state from DB". There is no recovery routine to test, which is the
design: the loop keeps nothing between ticks, so a restart *is* the next tick.
What has to be proven is that replaying a tick writes nothing twice — the two
unique constraints are the mechanism, and the fakes enforce the real ones.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from sentinel.bot.models import SignalDecision, SignalRecord, SignalStatus
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Settings
from sentinel.ingestion.models import OHLCVSeries
from sentinel.risk.models import PauseReason, TradePlan
from sentinel.tracker.loop import TrackerLoop
from sentinel.tracker.prices import PriceFeed
from tests.bot_double import OWNER_ID, owner_account
from tests.tracker_double import (
    FakeDatabase,
    FakeSignalRow,
    FakeStore,
    ScriptedFeed,
    fake_repositories,
)

from .conftest import LATER, candle

TICK = LATER + timedelta(minutes=1)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def loop(
    store: FakeStore, settings: Settings, feed: ScriptedFeed, *, now: object = TICK
) -> TrackerLoop:
    return TrackerLoop(
        FakeDatabase(store),  # type: ignore[arg-type]
        PriceFeed(feed, settings.config.tracker),
        settings,
        clock=FrozenClock(now),  # type: ignore[arg-type]
        repositories=fake_repositories(),
    )


def signal(store: FakeStore, plan: TradePlan, **overrides: object) -> FakeSignalRow:
    return store.add_signal(SignalRecord(plan=plan, user_id=OWNER_ID, number=1), **overrides)


# --------------------------------------------------------------------------- #
# A tick that does something
# --------------------------------------------------------------------------- #


async def test_a_tick_records_the_fill_it_detects(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    row = signal(store, plan)
    feed = ScriptedFeed([candle(open="83.40", high="83.45", low="83.05", close="83.35")])

    result = await loop(store, settings, feed).tick()

    assert result.checked == 1 and result.events == 1
    assert store.fills[(row.id, 0)]["price"] == Decimal("83.10")
    assert row.status == SignalStatus.PARTIALLY_FILLED.value
    assert row.filled_qty == Decimal("18.30")
    assert row.last_checked_at == TICK


async def test_a_stop_out_writes_the_realized_figures_and_the_outcome(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    row = signal(store, plan)
    feed = ScriptedFeed([candle(open="83.40", high="83.45", low="81.10", close="81.30")])

    result = await loop(store, settings, feed).tick()

    assert result.resolved == 1
    assert row.status == SignalStatus.STOPPED.value
    assert row.outcome == "STOP"
    # The whole ladder filled on the way down, so the whole budget went.
    assert row.realized_r == Decimal("-1.00")
    assert row.closed_at is not None
    # M5.1 §10: what it actually cost, not what the plan estimated.
    assert row.realized_costs_eur == Decimal("3.16")


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


async def test_running_the_same_tick_twice_writes_nothing_twice(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """The crash window: the process dies after the fill row and before the status
    update, and the next tick sees the same candle again."""
    signal(store, plan)
    feed = ScriptedFeed([candle(open="83.40", high="83.45", low="83.05", close="83.35")])

    first = await loop(store, settings, feed).tick()
    fills_after_first = dict(store.fills)
    events_after_first = dict(store.events)

    second = await loop(store, settings, feed).tick()

    assert first.events == 1
    assert second.events == 0, "the replayed fill must be refused, not re-journalled"
    assert store.fills == fills_after_first
    assert store.events == events_after_first


async def test_state_is_rebuilt_from_the_database_and_not_from_memory(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """A fresh ``TrackerLoop`` — the restart — continues where the last one left
    off, because everything it needs is in the two leg tables."""
    row = signal(store, plan)

    filling = ScriptedFeed([candle(open="83.40", high="83.45", low="83.05", close="83.35")])
    await loop(store, settings, filling).tick()
    assert row.status == SignalStatus.PARTIALLY_FILLED.value

    # A different loop object, as after a restart. It has never seen this signal.
    stopping = ScriptedFeed([candle(open="81.90", high="81.95", low="81.15", close="81.60")])
    await loop(store, settings, stopping, now=TICK + timedelta(minutes=5)).tick()

    assert row.status == SignalStatus.STOPPED.value
    # Rungs 2 and 3 filled on the way to the stop, so this is the full -1.00R.
    assert row.realized_r == Decimal("-1.00")


async def test_a_terminal_signal_is_not_looked_at_again(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    signal(store, plan, status=SignalStatus.STOPPED.value)
    feed = ScriptedFeed([candle(open="83.40", high="83.45", low="82.05", close="82.30")])

    result = await loop(store, settings, feed).tick()

    assert result.checked == 0
    assert feed.calls == [], "a closed signal must not cost an exchange call"


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


async def test_one_symbol_failing_does_not_stop_the_others(
    store: FakeStore, settings: Settings, plan: TradePlan, short_plan: TradePlan
) -> None:
    """PRD F1's rule, applied per signal. A symbol whose candles will not load is
    skipped and logged; the rest of the book is still managed."""
    broken = signal(store, plan)
    healthy = signal(store, short_plan)
    broken_symbol = broken.symbol

    class HalfBroken(ScriptedFeed):
        async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries:
            if symbol == broken_symbol and not self.calls:
                self.calls.append((symbol, timeframe, limit))
                raise RuntimeError("exchange timeout")
            return await super().ohlcv(symbol, timeframe, limit)

    feed = HalfBroken([candle(open="83.40", high="83.75", low="83.35", close="83.60")])
    result = await loop(store, settings, feed).tick()

    assert result.skipped == 1
    assert result.failures and "exchange timeout" in result.failures[0]
    assert healthy.status == SignalStatus.PARTIALLY_FILLED.value


# --------------------------------------------------------------------------- #
# The rail that runs on every tick (§7)
# --------------------------------------------------------------------------- #


async def test_a_losing_day_pauses_that_user(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """§7's limit is 3% of capital. Four taken signals closed today at -0.75% of
    a EUR 10,000 account each is -3.0%, which reaches it.

    Per user from M8.1: the pause lands on the ``users`` row, because the limit is a
    percentage of *somebody's* capital and there is no longer one capital. The
    operator's system-wide ``/pause`` is a different row and is untouched.
    """
    store.users[OWNER_ID] = owner_account(capital_eur=Decimal("10000"))
    for _ in range(4):
        row = signal(store, plan)
        row.decision = SignalDecision.TAKEN.value
        row.status = SignalStatus.STOPPED.value
        row.closed_at = TICK
        row.realized_eur = Decimal("-75")

    result = await loop(store, settings, ScriptedFeed()).tick()

    assert result.paused_users == [OWNER_ID]
    paused = store.users[OWNER_ID].pause
    assert paused.paused is True
    assert paused.reason is PauseReason.DAILY_LOSS_LIMIT
    assert paused.until == TICK + timedelta(hours=24)
    assert store.pause.paused is False, "the operator's own /pause is a separate rail"


async def test_a_recovered_day_does_not_pause(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """§7 limits the *realized* daily loss, so an afternoon that gives most of a
    bad morning back is not a 3% day."""
    store.users[OWNER_ID] = owner_account(capital_eur=Decimal("10000"))
    for amount in ("-150", "-150", "270"):
        row = signal(store, plan)
        row.decision = SignalDecision.TAKEN.value
        row.status = SignalStatus.STOPPED.value
        row.closed_at = TICK
        row.realized_eur = Decimal(amount)

    result = await loop(store, settings, ScriptedFeed()).tick()

    assert result.paused_users == []
    assert store.users[OWNER_ID].pause.paused is False


async def test_a_paper_loss_cannot_pause_a_real_account(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """Dry-run signals are tracked and measured, and they commit nothing. A bad
    rehearsal day must not stop the system it is rehearsing for."""
    store.users[OWNER_ID] = owner_account(capital_eur=Decimal("10000"))
    for _ in range(4):
        row = signal(store, plan, dry_run=True)
        row.decision = SignalDecision.TAKEN.value
        row.status = SignalStatus.STOPPED.value
        row.closed_at = TICK
        row.realized_eur = Decimal("-75")

    result = await loop(store, settings, ScriptedFeed()).tick()

    assert result.paused_users == []
    assert store.users[OWNER_ID].pause.paused is False


async def test_the_pause_is_not_re_raised_on_every_later_tick(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """One notice per pause. Re-saving it every 60 seconds would extend the 24h
    window forever and post the §4 message on every tick."""
    store.users[OWNER_ID] = owner_account(capital_eur=Decimal("10000"))
    row = signal(store, plan)
    row.decision = SignalDecision.TAKEN.value
    row.status = SignalStatus.STOPPED.value
    row.closed_at = TICK
    row.realized_eur = Decimal("-400")

    first = await loop(store, settings, ScriptedFeed()).tick()
    until = store.users[OWNER_ID].pause.until

    second = await loop(store, settings, ScriptedFeed(), now=TICK + timedelta(minutes=1)).tick()

    assert first.paused_users == [OWNER_ID]
    assert second.paused_users == []
    assert store.users[OWNER_ID].pause.until == until, (
        "the 24h window must not slide forward each tick"
    )


async def test_a_signal_older_than_the_tracker_is_replayed_from_its_creation(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """The defect the first live run exposed, as a test.

    Signal #41 was published by M6 and sat untouched until M7's tracker existed.
    Its ``last_checked_at`` was NULL, so the window fell back to the five-candle
    floor and the tracker judged it on the aftermath. A first tick must ask for the
    whole life of the signal instead.
    """
    row = signal(store, plan)
    assert row.last_checked_at is None

    feed = ScriptedFeed()
    hours_later = TICK + timedelta(hours=3)
    await loop(store, settings, feed, now=hours_later).tick()

    _, timeframe, limit = feed.calls[0]
    assert timeframe == "1m"
    assert limit > settings.config.tracker.fill_lookback_candles
    # Three hours of 1m candles, plus the partly-covered one at the boundary.
    assert limit >= 180
