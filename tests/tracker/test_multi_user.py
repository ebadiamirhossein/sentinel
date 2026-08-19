"""The tracker under several users (M8.1).

The tracker itself stays global — it follows everybody's signals, because a fill is
a fact about the market. What becomes per user is the *consequence*: whose daily
loss limit was reached, whose capital it is a percentage of, and who hears about it.

Plus one thing that is nobody's consequence and everybody's cost: with one shared
analysis producing one signal per user, several open signals now routinely ask the
exchange the identical question every 60 seconds.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from sentinel.bot.models import SignalDecision, SignalRecord, SignalStatus
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Settings
from sentinel.risk.models import PauseReason, PauseState, TradePlan
from sentinel.tracker.loop import TrackerLoop
from sentinel.tracker.prices import PriceFeed
from tests.bot_double import member_account, owner_account
from tests.tracker_double import FakeDatabase, FakeStore, ScriptedFeed, fake_repositories

from .conftest import LATER

OWNER = 111
MEMBER = 222
TICK = LATER


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    store.users[OWNER] = owner_account(OWNER, capital_eur=Decimal("10000"))
    store.users[MEMBER] = member_account(MEMBER, capital_eur=Decimal("2000"))
    return store


def stopped(store: FakeStore, plan: TradePlan, user_id: int, realized_eur: str) -> None:
    row = store.add_signal(SignalRecord(plan=plan, user_id=user_id, number=1))
    row.decision = SignalDecision.TAKEN.value
    row.status = SignalStatus.STOPPED.value
    row.closed_at = TICK
    row.realized_eur = Decimal(realized_eur)


def loop(store: FakeStore, settings: Settings) -> TrackerLoop:
    return TrackerLoop(
        FakeDatabase(store),  # type: ignore[arg-type]
        PriceFeed(ScriptedFeed(), settings.config.tracker),
        settings,
        clock=FrozenClock(TICK),
        repositories=fake_repositories(),
    )


async def test_the_loss_limit_is_measured_against_each_users_own_capital(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """The reason this rail had to become per user at all.

    €300 is 3% of the member's €2,000 and 3% of the owner's €10,000 would be €300
    too — but the owner only lost €100. One limit, two denominators, one pause.
    """
    stopped(store, plan, OWNER, "-100")
    stopped(store, plan, MEMBER, "-300")

    result = await loop(store, settings).tick()

    assert result.paused_users == [MEMBER]
    assert store.users[MEMBER].pause.reason is PauseReason.DAILY_LOSS_LIMIT
    assert store.users[OWNER].pause.paused is False, "a bad day for one is not a bad day for all"


async def test_a_pause_holds_only_the_user_it_belongs_to(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """And never the operator's system-wide rail, which is a different row."""
    stopped(store, plan, MEMBER, "-300")

    await loop(store, settings).tick()

    assert store.pause.paused is False, "risk_state stays the operator's /pause alone"


async def test_a_user_who_left_mid_day_is_not_paused(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """Their signals still resolve — the measurement is theirs and stays theirs —
    but a pause on a book nothing will be delivered to is bookkeeping for its own
    sake."""
    del store.users[MEMBER]
    stopped(store, plan, MEMBER, "-300")

    result = await loop(store, settings).tick()

    assert result.paused_users == []


async def test_both_users_can_be_paused_by_their_own_books(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    stopped(store, plan, OWNER, "-400")
    stopped(store, plan, MEMBER, "-300")

    result = await loop(store, settings).tick()

    assert result.paused_users == [OWNER, MEMBER]
    assert result.paused is True


async def test_a_user_with_no_capital_has_no_denominator_and_is_not_paused(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """M7's ruling, unchanged: without capital there is no percentage, and the gate
    already rejects everything with ``NO_CAPITAL`` in that state."""
    store.users[MEMBER] = member_account(MEMBER)
    stopped(store, plan, MEMBER, "-9999")

    result = await loop(store, settings).tick()

    assert result.paused_users == []


async def test_an_existing_pause_is_not_re_raised_for_that_user(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    """Re-saving it every 60 seconds would slide the 24h window forward forever and
    re-post the §4 notice every minute — M7's rule, now per user."""
    already = PauseState(
        paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=TICK + timedelta(hours=12)
    )
    store.users[MEMBER] = member_account(MEMBER, capital_eur=Decimal("2000"), pause=already)
    stopped(store, plan, MEMBER, "-300")

    result = await loop(store, settings).tick()

    assert result.paused_users == []
    assert store.users[MEMBER].pause.until == already.until


# --------------------------------------------------------------------------- #
# The exchange sees one question per symbol per tick
# --------------------------------------------------------------------------- #


async def test_several_users_signals_on_one_symbol_cost_one_candle_request(
    settings: Settings, plan: TradePlan
) -> None:
    """Four users holding the same shared analysis is four open signals asking the
    identical thing. Without the per-tick cache that is four calls a minute to a
    rate-limited public endpoint, growing with the number of friends."""
    source = ScriptedFeed()
    feed = PriceFeed(source, settings.config.tracker)

    for _ in range(4):
        await feed.fill_candles("SOLUSDT", since=None, now=TICK)

    assert len(source.calls) == 1


async def test_the_cache_does_not_survive_the_tick(settings: Settings, plan: TradePlan) -> None:
    """Candles go stale in exactly one minute. A feed that remembered them across
    ticks would be the polled-mark-price bug M7 removed, reintroduced as a cache."""
    source = ScriptedFeed()
    feed = PriceFeed(source, settings.config.tracker)

    await feed.fill_candles("SOLUSDT", since=None, now=TICK)
    feed.reset()
    await feed.fill_candles("SOLUSDT", since=None, now=TICK)

    assert len(source.calls) == 2


async def test_different_symbols_are_still_fetched_separately(
    settings: Settings, plan: TradePlan
) -> None:
    """Proof the cache key is the whole question and not just the timeframe."""
    source = ScriptedFeed()
    feed = PriceFeed(source, settings.config.tracker)

    await feed.fill_candles("SOLUSDT", since=None, now=TICK)
    await feed.fill_candles("BTCUSDT", since=None, now=TICK)

    assert len(source.calls) == 2


async def test_a_tick_clears_the_cache_before_it_reads_anything(
    store: FakeStore, settings: Settings, plan: TradePlan
) -> None:
    source = ScriptedFeed()
    feed = PriceFeed(source, settings.config.tracker)
    await feed.fill_candles("SOLUSDT", since=None, now=TICK)

    tracker = TrackerLoop(
        FakeDatabase(store),  # type: ignore[arg-type]
        feed,
        settings,
        clock=FrozenClock(TICK),
        repositories=fake_repositories(),
    )
    store.add_signal(SignalRecord(plan=plan, user_id=OWNER, number=1))
    await tracker.tick()

    assert len(source.calls) > 1, "the tick must not reuse the previous tick's candles"
