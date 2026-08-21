"""Tracking a market that is shut 49 hours a week (FOREX.md §5.4, §16.10).

Three behaviours with no 24/7 analogue at all, and the first is the one that would
never have been found by watching for errors, because it produces none.

1. **A closed market is not a stall.** Ticking through a weekend asks a shut venue the
   same question ~2,940 times, logs a capped lookback every minute because the window
   since the last tick spans two days, and re-walks Friday's bars as though they were
   news. All of that is *quiet*. It would simply have been the majority of what the
   tracker did.
2. **No pending ladder can survive the weekend**, because the gate caps every forex
   expiry at the Friday close — so the "closed hours count against a TTL" problem is
   removed at plan time rather than discounted at detection time.
3. **A currency-matched high-impact event cancels a pending ladder** (§8), with a
   reason the owner sees. An open position is annotated and never closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.analyst.models import TimeframeLabel
from sentinel.bot.models import SignalRecord, SignalStatus
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings
from sentinel.core.markets import Market
from sentinel.fx.expiry import friday_ladder_expiry, plan_expiry
from sentinel.fx.hours import MarketState, state_at
from sentinel.fx.plan import ForexPlan
from sentinel.tracker.loop import TrackerLoop
from sentinel.tracker.prices import PriceFeed
from tests.bot_double import owner_account
from tests.fx.forex_double import FX_NOW, analyst_report, calendar, decide, forex_plan, high_impact
from tests.tracker_double import FakeDatabase, FakeStore, ScriptedFeed, fake_repositories

OWNER = 111

#: A Saturday. The venue is shut and the candle series simply has no bars for it.
CLOSED = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings(repo_config: AppConfig) -> Settings:
    return Settings(secrets=Secrets(_env_file=None), config=repo_config)


@pytest.fixture
def plan(repo_config: AppConfig) -> ForexPlan:
    return forex_plan(repo_config)


@pytest.fixture
def store(plan: ForexPlan) -> FakeStore:
    store = FakeStore()
    store.users[OWNER] = owner_account(OWNER, capital_eur=Decimal("10000"))
    store.add_signal(SignalRecord(plan=plan, user_id=OWNER, number=1, market=Market.FOREX))
    return store


def loop(
    store: FakeStore, settings: Settings, *, now: datetime, events: object = None
) -> TrackerLoop:
    return TrackerLoop(
        FakeDatabase(store),  # type: ignore[arg-type]
        PriceFeed(ScriptedFeed(), settings.config.tracker),
        settings,
        market=Market.FOREX,
        clock=FrozenClock(now),
        repositories=fake_repositories(),
        calendar=events if events is not None else calendar(),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# 1 — closed is not a stall
# --------------------------------------------------------------------------- #


async def test_a_closed_market_skips_the_tick_entirely(
    store: FakeStore, settings: Settings
) -> None:
    result = await loop(store, settings, now=CLOSED).tick()

    assert result.closed is True
    assert result.checked == 0
    assert result.failures == []


async def test_a_closed_tick_asks_the_venue_nothing(store: FakeStore, settings: Settings) -> None:
    """The cost this is really about: 49 hours a week of pointless requests.

    Asserted on the feed rather than on the result, because "checked 0" would also be
    true of a tick that fetched every candle and found nothing to do.
    """
    feed = ScriptedFeed()
    tracker = TrackerLoop(
        FakeDatabase(store),  # type: ignore[arg-type]
        PriceFeed(feed, settings.config.tracker),
        settings,
        market=Market.FOREX,
        clock=FrozenClock(CLOSED),
        repositories=fake_repositories(),
        calendar=calendar(),
    )

    await tracker.tick()

    assert feed.calls == [], feed.calls


async def test_an_open_market_ticks_normally(store: FakeStore, settings: Settings) -> None:
    """The non-vacuity sibling — otherwise "skips everything" would pass this file."""
    result = await loop(store, settings, now=FX_NOW).tick()

    assert result.closed is False
    assert result.checked == 1


async def test_crypto_has_no_closed_state_at_all(store: FakeStore, settings: Settings) -> None:
    """The rail is forex's, and a 24/7 market must never acquire it by accident."""
    crypto_store = FakeStore()
    tracker = TrackerLoop(
        FakeDatabase(crypto_store),  # type: ignore[arg-type]
        PriceFeed(ScriptedFeed(), settings.config.tracker),
        settings,
        market=Market.CRYPTO,
        clock=FrozenClock(CLOSED),
        repositories=fake_repositories(),
    )

    result = await tracker.tick()

    assert result.closed is False


def test_the_weekend_really_is_closed(repo_config: AppConfig) -> None:
    """The fixture's premise, stated: a Saturday noon is CLOSED and a Wednesday is not."""
    assert state_at(CLOSED, repo_config.forex) is MarketState.CLOSED
    assert state_at(FX_NOW, repo_config.forex).is_open


# --------------------------------------------------------------------------- #
# 2 — no pending ladder can reach the weekend
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "created",
    [
        datetime(2026, 8, 10, 8, 0, tzinfo=UTC),  # Monday
        datetime(2026, 8, 12, 12, 0, tzinfo=UTC),  # Wednesday
        datetime(2026, 8, 13, 23, 0, tzinfo=UTC),  # Thursday night
        datetime(2026, 8, 14, 12, 0, tzinfo=UTC),  # Friday midday
    ],
)
@pytest.mark.parametrize(
    "label", [TimeframeLabel.INTRADAY, TimeframeLabel.SWING, TimeframeLabel.NONE]
)
def test_no_ladder_can_ever_rest_over_a_weekend(
    repo_config: AppConfig, created: datetime, label: TimeframeLabel
) -> None:
    """§5.4, as a property over every day of the week and every TTL.

    This is *why* the tracker needs no weekend-discounted TTL: the deadline is decided
    once, at gate time, and recorded with the reason it was chosen. A detector that
    subtracted closed hours on every tick would be a second implementation of the same
    rule, running 1,440 times a day, able to disagree with the number on the card.
    """
    at, _ = plan_expiry(
        created_at=created,
        timeframe_label=label,
        management=repo_config.management,
        forex=repo_config.forex,
    )

    assert at <= friday_ladder_expiry(created, repo_config.forex)
    assert state_at(at, repo_config.forex).is_open, f"{at} is not inside the trading week"


def test_the_expiry_says_which_deadline_ended_it(repo_config: AppConfig) -> None:
    """ "Expired unfilled" is true of both and useful for neither."""
    friday = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    monday = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)

    _, by_week = plan_expiry(
        created_at=friday,
        timeframe_label=TimeframeLabel.INTRADAY,
        management=repo_config.management,
        forex=repo_config.forex,
    )
    _, by_ttl = plan_expiry(
        created_at=monday,
        timeframe_label=TimeframeLabel.INTRADAY,
        management=repo_config.management,
        forex=repo_config.forex,
    )

    assert "Friday" in by_week
    assert "TTL" in by_ttl
    assert by_week != by_ttl


# --------------------------------------------------------------------------- #
# 3 — a blackout cancels a pending ladder (§8)
# --------------------------------------------------------------------------- #


async def test_a_high_impact_event_cancels_a_pending_ladder(
    store: FakeStore, settings: Settings, plan: ForexPlan
) -> None:
    """Cancelled, not paused: a ladder resting through a rate decision is an order
    placed on the assumption that nothing has changed."""
    events = calendar(events=(high_impact(at=FX_NOW + timedelta(minutes=20), currency="USD"),))

    result = await loop(store, settings, now=FX_NOW, events=events).tick()

    row = next(iter(store.signals.values()))
    assert row.status == SignalStatus.EXPIRED.value
    assert result.resolved == 1


async def test_the_cancellation_says_why(store: FakeStore, settings: Settings) -> None:
    """A run of "expired unfilled" a reader cannot tell from a time stop teaches
    nothing. The journal entry names the cause."""
    events = calendar(events=(high_impact(at=FX_NOW + timedelta(minutes=20), currency="USD"),))

    await loop(store, settings, now=FX_NOW, events=events).tick()

    row = next(iter(store.signals.values()))
    recorded = store.events_for(row.id)
    assert any("high-impact event" in entry["detail"] for entry in recorded), recorded


async def test_an_event_in_an_unrelated_currency_cancels_nothing(
    store: FakeStore, settings: Settings
) -> None:
    """The non-vacuity sibling. EURUSD is exposed to EUR and USD, and nothing else."""
    events = calendar(events=(high_impact(at=FX_NOW + timedelta(minutes=20), currency="JPY"),))

    await loop(store, settings, now=FX_NOW, events=events).tick()

    row = next(iter(store.signals.values()))
    assert row.status == SignalStatus.PENDING_ENTRY.value


async def test_an_open_position_is_never_closed_by_a_blackout(
    settings: Settings, repo_config: AppConfig
) -> None:
    """§8: annotate an open position, never close it. Sentinel informs, it does not
    instruct — and here that falls out of the state machine, which refuses to expire a
    signal that has filled rather than needing a second rule to say so.
    """
    decision = decide(repo_config, report=analyst_report(timeframe_label=TimeframeLabel.SWING))
    assert decision.plan is not None
    store = FakeStore()
    store.users[OWNER] = owner_account(OWNER, capital_eur=Decimal("10000"))
    row = store.add_signal(
        SignalRecord(plan=decision.plan, user_id=OWNER, number=1, market=Market.FOREX)
    )
    row.status = SignalStatus.FILLED.value
    store.fills[(row.id, 0)] = {
        "signal_id": row.id,
        "rung_index": 0,
        "price": decision.plan.entries[0].price,
        "qty": decision.plan.entries[0].qty,
        "filled_at": FX_NOW,
    }
    events = calendar(events=(high_impact(at=FX_NOW + timedelta(minutes=20), currency="USD"),))

    await loop(store, settings, now=FX_NOW, events=events).tick()

    assert store.signals[row.id].status == SignalStatus.FILLED.value
