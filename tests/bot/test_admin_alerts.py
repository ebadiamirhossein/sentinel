"""Delivery of admin alerts (M8) — including the ways it must not deliver.

``core/alerts.py`` decides *whether*; this is about *sending*, and the properties
that matter are the awkward ones: an alerter that raises would kill the scan job
it reports on, and an alerter that repeated itself every fifteen minutes would be
muted within a day and then never read again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.alerts import AdminAlerter
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Secrets, Settings, load_config
from sentinel.core.orchestrator import CycleResult
from sentinel.llm.spend import SpendState, SpendTotals
from tests.bot_double import FakeBot, FakeDatabase, FakeStore, fake_repositories

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
CHATS = (111, 222)


@dataclass
class FakeCycleRow:
    """The four columns ``AdminAlerter`` reads off a ``cycles`` row."""

    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


@pytest.fixture
def settings() -> Settings:
    return Settings(secrets=Secrets(_env_file=None), config=load_config())


def alerter(store: FakeStore, bot: FakeBot, settings: Settings) -> AdminAlerter:
    repos = fake_repositories()
    return AdminAlerter(
        FakeDatabase(store),  # type: ignore[arg-type]
        bot,
        chat_ids=CHATS,
        settings=settings,
        tz=ZoneInfo("Europe/Vilnius"),
        clock=FrozenClock(NOW),
        cycles=repos.cycles,
        llm_calls=repos.llm_calls,
    )


def failures(count: int) -> list[FakeCycleRow]:
    return [
        FakeCycleRow(
            status="FAILED",
            started_at=NOW - timedelta(minutes=15 * (index + 1)),
            finished_at=NOW - timedelta(minutes=15 * index),
            error="AnalystUnavailable: connection reset",
        )
        for index in range(count)
    ]


def result(**kwargs: object) -> CycleResult:
    return CycleResult(cycle_id=uuid4(), **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Cycle failures
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_three_failures_message_every_allowlisted_chat_once(settings: Settings) -> None:
    store = FakeStore(cycles=failures(3))
    bot = FakeBot()

    sent = await alerter(store, bot, settings).after_cycle(result())

    assert sent == len(CHATS)
    assert [call.kwargs["chat_id"] for call in bot.of("send_message")] == list(CHATS)
    text = bot.texts[0]
    assert "3 cycles failed in a row" in text
    assert "connection reset" in text
    # The line an owner with an open position needs first.
    assert "tracker is unaffected" in text


@pytest.mark.asyncio
async def test_two_failures_say_nothing(settings: Settings) -> None:
    bot = FakeBot()
    assert await alerter(FakeStore(cycles=failures(2)), bot, settings).after_cycle(result()) == 0
    assert bot.calls == []


@pytest.mark.asyncio
async def test_the_fourth_failure_does_not_repeat_the_message(settings: Settings) -> None:
    """Otherwise an overnight outage sends forty messages and gets muted."""
    bot = FakeBot()
    assert await alerter(FakeStore(cycles=failures(4)), bot, settings).after_cycle(result()) == 0
    assert bot.calls == []


@pytest.mark.asyncio
async def test_a_recovery_is_announced_once(settings: Settings) -> None:
    recovered = [
        FakeCycleRow(status="OK", started_at=NOW - timedelta(minutes=1), finished_at=NOW),
        *failures(3),
    ]
    bot = FakeBot()

    await alerter(FakeStore(cycles=recovered), bot, settings).after_cycle(result())

    assert "recovered" in bot.texts[0]


@pytest.mark.asyncio
async def test_a_telegram_outage_never_reaches_the_scheduler(settings: Settings) -> None:
    """The job being reported on must not die of the report failing.

    This is the whole reason ``after_cycle`` swallows: the alerter runs inside the
    scan job, and an exception here would stop the pipeline because Telegram was
    briefly unreachable.
    """
    bot = FakeBot(fail={"send_message": RuntimeError("Bad Gateway")})

    sent = await alerter(FakeStore(cycles=failures(3)), bot, settings).after_cycle(result())

    assert sent == 0
    assert len(bot.of("send_message")) == len(CHATS)  # both attempted, neither raised


# --------------------------------------------------------------------------- #
# The spend guard's notice (M7 promised it; M8 sends it)
# --------------------------------------------------------------------------- #


def spent(day: str) -> SpendTotals:
    return SpendTotals(day_usd=Decimal(day), month_usd=Decimal(day), calls=12)


@pytest.mark.asyncio
async def test_crossing_the_warn_level_sends_one_notice(settings: Settings) -> None:
    store = FakeStore(spend=spent("7.40"))
    bot = FakeBot()

    await alerter(store, bot, settings).after_cycle(
        result(spend_state_before=SpendState.OK, spend_state_after=SpendState.WARN)
    )

    assert "warn level" in bot.texts[0]
    assert "$7.40" in bot.texts[0]
    assert "Nothing is suspended yet" in bot.texts[0]


@pytest.mark.asyncio
async def test_reaching_the_limit_says_what_stops_and_what_does_not(settings: Settings) -> None:
    bot = FakeBot()

    await alerter(FakeStore(spend=spent("10.10")), bot, settings).after_cycle(
        result(spend_state_before=SpendState.WARN, spend_state_after=SpendState.LIMIT_REACHED)
    )

    text = bot.texts[0]
    assert "limit reached" in text
    assert "deep analysis is suspended" in text
    assert "screener and the tracker keep running" in text


@pytest.mark.asyncio
async def test_staying_at_the_same_level_is_not_news(settings: Settings) -> None:
    """The transition is the event. A day spent above the warn level would
    otherwise send a message every fifteen minutes until midnight."""
    bot = FakeBot()

    await alerter(FakeStore(spend=spent("8.00")), bot, settings).after_cycle(
        result(spend_state_before=SpendState.WARN, spend_state_after=SpendState.WARN)
    )

    assert bot.calls == []


@pytest.mark.asyncio
async def test_a_cycle_that_never_reached_the_guard_says_nothing(settings: Settings) -> None:
    """``spend_state_after`` is None when the cycle stopped before analysis —
    no key, no snapshots. That is not a crossing."""
    bot = FakeBot()

    await alerter(FakeStore(), bot, settings).after_cycle(
        result(spend_state_before=SpendState.OK, spend_state_after=None)
    )

    assert bot.calls == []


@pytest.mark.asyncio
async def test_an_unpriced_call_makes_the_figure_a_floor(settings: Settings) -> None:
    """journal/M7_REPORT.md §9's hole: an unpriced model is missing from the
    total, not free, and the message has to say so rather than understate."""
    store = FakeStore(
        spend=SpendTotals(
            day_usd=Decimal("7.10"), month_usd=Decimal("7.10"), calls=12, unpriced_calls=2
        )
    )
    bot = FakeBot()

    await alerter(store, bot, settings).after_cycle(
        result(spend_state_before=SpendState.OK, spend_state_after=SpendState.WARN)
    )

    assert "at least $7.10" in bot.texts[0]
    assert "not zero" in bot.texts[0]
