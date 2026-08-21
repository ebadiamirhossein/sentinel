"""§8's staleness rail, wired to a message for the first time (M10d).

**The rail existed and nothing read it.** ``EconomicCalendar.health`` was called only
from inside ``blackout``, so its ``warn_within_days`` branch composed a sentence that
never left the process. The calendar could quietly run out, the gate would start
rejecting every symbol with ``CALENDAR_STALE``, and the first sign would be that no
forex card had arrived for a while — silence reading as a quiet market, which is the
one failure mode this system cannot see.

So HANDOFF §4 item 1 governs this file: *every filter, handler and alert path needs a
POSITIVE reachability test on real machinery.* Nothing here asserts that a function
returned ``True``. Each test drives the real ``CycleOrchestrator`` method, the real
alert builder, the real read model, the real card renderer and a real ``UserNotifier``
with its real claim-and-confirm, and then reads the **text that landed in the bot's
outbox**.

Every assertion has a sibling that makes it capable of failing: a healthy calendar
sends nothing, a second cycle on the same day sends nothing, and the two states —
running out and run out — say different things.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.notices import UserNotifier, calendar_coverage_key
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings
from sentinel.core.markets import Market
from sentinel.core.orchestrator import CycleOrchestrator, CycleRepositories
from sentinel.fx.calendar import CalendarEvent, EconomicCalendar, Impact, load_calendar
from tests.bot_double import FakeBot, FakeMessageStore, owner_account
from tests.core.conftest import CycleDatabase, CycleStore, CycleUsers

OWNER = 7222549221
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
TZ = ZoneInfo("Europe/Vilnius")

pytestmark = pytest.mark.asyncio


def calendar_until(coverage: date, **kwargs: Any) -> EconomicCalendar:
    """A calendar with one real event, covered to ``coverage``."""
    return EconomicCalendar(
        events=(
            CalendarEvent(
                at=datetime(2026, 9, 16, 18, 0, tzinfo=UTC),
                currency="USD",
                impact=Impact.HIGH,
                name="FOMC decision",
                source="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                **kwargs,
            ),
        ),
        coverage_until=coverage,
        source="test",
    )


def orchestrator(
    config: AppConfig, calendar: EconomicCalendar, bot: FakeBot, *, now: datetime = NOW
) -> CycleOrchestrator:
    """The real orchestrator, with a real notifier over a fake bot.

    Only the database and the Telegram transport are doubled — the same boundary every
    other test in this suite draws. Everything between the calendar and the outbox is
    the code that runs in production.
    """
    store = CycleStore()
    store.users = [owner_account(OWNER, capital_eur=None)]
    database = CycleDatabase(store)
    settings = Settings(secrets=Secrets(_env_file=None), config=config)
    return CycleOrchestrator(
        settings,
        database,  # type: ignore[arg-type]
        market=Market.FOREX,
        clock=FrozenClock(now),
        calendar=calendar,
        notices=UserNotifier(
            database,  # type: ignore[arg-type]
            bot,
            telegram=config.telegram,
            clock=FrozenClock(now),
            messages=FakeMessageStore,
        ),
        repositories=CycleRepositories(users=CycleUsers),  # type: ignore[arg-type]
    )


def outbox(bot: FakeBot) -> list[str]:
    return [call.kwargs["text"] for call in bot.calls if call.method == "send_message"]


# --------------------------------------------------------------------------- #
# The alert reaches a chat
# --------------------------------------------------------------------------- #


async def test_coverage_running_out_reaches_the_owner_with_the_action_attached(
    repo_config: AppConfig,
) -> None:
    """Ten days left: still usable, still emitting, and the owner is told now.

    Asserted on the text rather than on a call count, because the whole reason this
    alert has its own kind is that it has to name the job. §3.1's lesson, one alert
    over: "the calendar is stale" would send him looking at data when the fix is one
    file and a rebuild.
    """
    bot = FakeBot()
    calendar = calendar_until(NOW.date() + timedelta(days=10))

    await orchestrator(repo_config, calendar, bot)._alert_calendar_coverage()

    sent = outbox(bot)
    assert len(sent) == 1, sent
    text = sent[0]
    assert "calendar" in text.lower()
    assert "10 day" in text
    assert "calendar.yaml" in text, "the alert does not name the file to edit"
    assert "--build" in text, (
        "the alert does not say a restart cannot pick this up — the calendar is "
        "COPY-ed into the image (journal/HYGIENE_2026-08-21.md §1)"
    )
    assert "Crypto is unaffected" in text


async def test_a_healthy_calendar_sends_nothing(repo_config: AppConfig) -> None:
    """The sibling that gives the test above teeth. Without it, an alerter that fired
    unconditionally would pass every assertion in this file."""
    bot = FakeBot()
    calendar = calendar_until(NOW.date() + timedelta(days=200))

    await orchestrator(repo_config, calendar, bot)._alert_calendar_coverage()

    assert outbox(bot) == []


async def test_lapsed_coverage_says_forex_is_emitting_nothing(repo_config: AppConfig) -> None:
    """Expiring and expired are **different states with different outcomes** (§8), and
    the messages have to differ or the distinction is only in the code.

    Expiring: "it will stop when coverage lapses." Expired: it already has.
    """
    bot = FakeBot()
    calendar = calendar_until(NOW.date() - timedelta(days=1))

    await orchestrator(repo_config, calendar, bot)._alert_calendar_coverage()

    sent = outbox(bot)
    assert len(sent) == 1
    assert "NOTHING" in sent[0]
    assert "run out" in sent[0]


async def test_the_two_states_do_not_send_the_same_message(repo_config: AppConfig) -> None:
    """Pinned as a comparison rather than as two literals, so rewording either one
    cannot quietly collapse them into the same text."""
    expiring, expired = FakeBot(), FakeBot()

    await orchestrator(
        repo_config, calendar_until(NOW.date() + timedelta(days=3)), expiring
    )._alert_calendar_coverage()
    await orchestrator(
        repo_config, calendar_until(NOW.date() - timedelta(days=3)), expired
    )._alert_calendar_coverage()

    assert outbox(expiring)[0] != outbox(expired)[0]


# --------------------------------------------------------------------------- #
# It nudges daily, not hourly
# --------------------------------------------------------------------------- #


async def test_a_second_cycle_on_the_same_day_sends_nothing(repo_config: AppConfig) -> None:
    """Twenty-four cycles a day for a fortnight is 336 messages, which is a message
    nobody reads — and an ignored rail is the silence it exists to prevent.

    The claim goes through the real ``telegram_messages`` key, so this is the same
    mechanism the daily-loss notice uses rather than a counter somewhere.
    """
    bot = FakeBot()
    calendar = calendar_until(NOW.date() + timedelta(days=5))
    engine = orchestrator(repo_config, calendar, bot)

    await engine._alert_calendar_coverage()
    await engine._alert_calendar_coverage()

    assert len(outbox(bot)) == 1


async def test_the_next_day_nudges_again(repo_config: AppConfig) -> None:
    """The other half: daily means daily. A key that never rolled would send once and
    then go quiet for the whole fortnight it is supposed to be counting down."""
    bot = FakeBot()
    calendar = calendar_until(NOW.date() + timedelta(days=5))
    store = CycleStore()
    store.users = [owner_account(OWNER, capital_eur=None)]

    assert calendar_coverage_key(NOW) != calendar_coverage_key(NOW + timedelta(days=1))

    await orchestrator(repo_config, calendar, bot, now=NOW)._alert_calendar_coverage()
    await orchestrator(
        repo_config, calendar, bot, now=NOW + timedelta(days=1)
    )._alert_calendar_coverage()

    assert len(outbox(bot)) == 2


# --------------------------------------------------------------------------- #
# The markers are read, not decorative
# --------------------------------------------------------------------------- #


async def test_the_alert_repeats_what_the_file_says_it_does_not_cover(
    repo_config: AppConfig,
) -> None:
    """``known_gaps`` and ``needs_verification`` exist to be asked about.

    A marker that only ever sits in a YAML comment is decoration. US PCE is absent
    from the shipped calendar because bea.gov could not be reached and no date was
    guessed — so the absence has to be visible somewhere a human looks, and the one
    message this system sends about the calendar is that place.
    """
    bot = FakeBot()
    calendar = EconomicCalendar(
        events=calendar_until(NOW.date(), needs_verification=True).events,
        coverage_until=NOW.date() + timedelta(days=5),
        source="test",
        known_gaps=("US PCE — bea.gov/news/schedule not reachable; no date guessed",),
    )

    await orchestrator(repo_config, calendar, bot)._alert_calendar_coverage()

    text = outbox(bot)[0]
    assert "PCE" in text
    assert "bea.gov" in text
    assert "FOMC decision" in text, "the unverified event is not named"


async def test_the_shipped_calendar_is_quiet_today_and_will_speak_before_it_lapses(
    repo_config: AppConfig,
) -> None:
    """The shipped file, driven through the real path at two real instants.

    This is the assertion switch-on actually rests on: the calendar committed today is
    healthy enough to say nothing, **and** the rail is not simply mute — it fires
    fourteen days before coverage ends, which is `calendar_warn_within_days`.
    """
    calendar = load_calendar()
    assert calendar.coverage_until is not None
    quiet_day = datetime.combine(
        calendar.coverage_until - timedelta(days=30), datetime.min.time(), tzinfo=UTC
    )
    warning_day = datetime.combine(
        calendar.coverage_until - timedelta(days=3), datetime.min.time(), tzinfo=UTC
    )

    quiet_bot, warning_bot = FakeBot(), FakeBot()
    await orchestrator(repo_config, calendar, quiet_bot, now=quiet_day)._alert_calendar_coverage()
    await orchestrator(
        repo_config, calendar, warning_bot, now=warning_day
    )._alert_calendar_coverage()

    assert outbox(quiet_bot) == []
    assert len(outbox(warning_bot)) == 1
    assert "PCE" in outbox(warning_bot)[0], "the shipped file's known gap is not repeated"
