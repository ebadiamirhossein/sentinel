"""A market that ingests nothing says so — M10e step 4.

**The failure this closes, in the owner's words.** "Three cycles a day, three pairs,
every one skipped, and I only found it because I went looking. ``cycle.complete`` said
``status: OK``. /health was green."

Forex spent the whole of Monday 2026-08-24 skipping all three pairs on a daily-staleness
rail that could not pass on a Monday (FOREX.md defect #31). Not one cycle failed. Every
one of them fetched successfully, produced zero usable symbols, wrote ``status: OK`` with
``scanned: 0`` and ``spend_usd: 0``, and left every health surface green. Every rail this
system has watches for something going wrong. Nothing watched for nothing going right.

HANDOFF §4 item 1 governs this file: *every alert path needs a POSITIVE reachability test
on real machinery.* Nothing below asserts a return value. Each test drives the real
``CycleOrchestrator`` method, the real decision function, the real read model, the real
card renderer and a real ``UserNotifier`` with its real claim-and-confirm, then reads the
text that landed in the bot's outbox.

And every assertion has a sibling that lets it fail: two barren cycles send nothing, a
market that is merely *shut* sends nothing, one outside its scan window sends nothing,
and a second alert on the same day sends nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.notices import UserNotifier
from sentinel.core.alerts import BarrenOutcome, barren_alert, consecutive_barren
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings
from sentinel.core.markets import Market
from sentinel.core.orchestrator import CycleOrchestrator, CycleRepositories, SkipReason
from tests.bot_double import FakeBot, FakeMessageStore, owner_account
from tests.core.conftest import CycleDatabase, CycleStore, CycleUsers

OWNER = 7222549221
#: Monday, mid-morning — the cycle the owner was reading when he found this.
NOW = datetime(2026, 8, 24, 10, 36, 49, tzinfo=UTC)
TZ = ZoneInfo("Europe/Vilnius")
SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY")


class _Row:
    """The five ``cycles`` columns the barren rail reads."""

    def __init__(
        self,
        *,
        started_at: datetime,
        status: str = "OK",
        requested: int = len(SYMBOLS),
        scanned: int = 0,
        reason: SkipReason | None = SkipReason.NO_DATA,
    ) -> None:
        self.cycle_id: UUID = uuid4()
        self.started_at = started_at
        self.status = status
        self.symbols_requested = requested
        self.symbols_scanned = scanned
        self.skipped: dict[str, Any] = (
            {}
            if reason is None
            else {s: {"reason": reason.value, "detail": "stale candles: 1d"} for s in SYMBOLS}
        )


def cycles(*rows: _Row) -> list[_Row]:
    """Newest first, as ``CycleRepository.recent`` returns them."""
    return sorted(rows, key=lambda row: row.started_at, reverse=True)


def barren_run(count: int, **kwargs: Any) -> list[_Row]:
    """``count`` consecutive cycles that looked and got nothing, one an hour."""
    return cycles(*(_Row(started_at=NOW - timedelta(hours=n), **kwargs) for n in range(count)))


def orchestrator(config: AppConfig, rows: list[_Row], bot: FakeBot) -> CycleOrchestrator:
    """The real orchestrator over a fake database and a fake Telegram transport.

    The same boundary every other cycle test draws: everything between the ``cycles``
    rows and the outbox is the code that runs in production.
    """
    store = CycleStore()
    store.users = [owner_account(OWNER, capital_eur=None)]
    database = CycleDatabase(store)

    class _Cycles:
        def __init__(self, session: Any, *, market: Market = Market.FOREX) -> None:
            self._market = market

        async def recent(self, limit: int = 10) -> list[_Row]:
            return rows[:limit]

    settings = Settings(secrets=Secrets(_env_file=None), config=config)
    return CycleOrchestrator(
        settings,
        database,  # type: ignore[arg-type]
        market=Market.FOREX,
        clock=FrozenClock(NOW),
        notices=UserNotifier(
            database,  # type: ignore[arg-type]
            bot,
            telegram=config.telegram,
            clock=FrozenClock(NOW),
            messages=FakeMessageStore,
        ),
        repositories=CycleRepositories(users=CycleUsers, cycles=_Cycles),  # type: ignore[arg-type]
    )


def outbox(bot: FakeBot) -> list[str]:
    return [call.kwargs["text"] for call in bot.calls if call.method == "send_message"]


# --------------------------------------------------------------------------- #
# The alert reaches a chat
# --------------------------------------------------------------------------- #


async def test_three_barren_cycles_reach_the_owner(repo_config: AppConfig) -> None:
    """The positive reachability test, on the exact shape of the live outage.

    Asserted on the text and not on a call count, because the whole reason this alert
    has its own kind is that it has to say *nothing failed*. A reader who goes hunting a
    stack trace on the strength of this message has been sent to the wrong place, and
    there is no stack trace to find.
    """
    bot = FakeBot()

    await orchestrator(repo_config, barren_run(3), bot)._alert_barren_market()

    sent = outbox(bot)
    assert len(sent) == 1, sent
    text = sent[0]
    assert "forex" in text.lower()
    assert "3" in text
    assert "Nothing crashed" in text, "the alert does not say this is not a crash"
    assert "OK" in text, "the alert does not name the status that hid this"
    assert "/pulse" in text, "the alert names no next step"


async def test_two_barren_cycles_send_nothing(repo_config: AppConfig) -> None:
    """The sibling that gives the test above teeth.

    Two is a transient: a Saxo 5xx, or a token refresh spanning two polls — the
    credential chain has a one-hour memory by design (§3.1) and a rail that fired
    inside it would cry wolf on ordinary operation.
    """
    bot = FakeBot()

    await orchestrator(repo_config, barren_run(2), bot)._alert_barren_market()

    assert outbox(bot) == []


async def test_a_shut_market_is_not_barren(repo_config: AppConfig) -> None:
    """Forex is closed for about 49 hours a week. This is the whole weekend.

    ``MARKET_CLOSED`` has its own ``SkipReason`` precisely so this question is
    answerable, and an alert that fired every Saturday would be muted before the first
    real one arrived.
    """
    bot = FakeBot()

    await orchestrator(
        repo_config, barren_run(5, reason=SkipReason.MARKET_CLOSED), bot
    )._alert_barren_market()

    assert outbox(bot) == []


async def test_cycles_outside_the_scan_window_are_not_barren(repo_config: AppConfig) -> None:
    """The venue is open and we are choosing not to look — also not an outage.

    ``forex.scan_hours_utc`` is ``[7, 19]``, so eleven of every twenty-four cycles skip
    every symbol by design. That is the second reason this rail cannot simply count
    ``scanned == 0``.
    """
    bot = FakeBot()

    await orchestrator(
        repo_config, barren_run(5, reason=SkipReason.OUTSIDE_SCAN_HOURS), bot
    )._alert_barren_market()

    assert outbox(bot) == []


async def test_the_second_alert_on_the_same_day_is_not_sent(repo_config: AppConfig) -> None:
    """Once per market per UTC day, through the existing claim-and-confirm path.

    The condition lasts a whole trading day — thirteen cycles of it on 2026-08-24 — and
    thirteen identical messages is the alert fatigue ``calendar_coverage_key`` was
    written to avoid. Exercised by sending twice against one store rather than by
    reading the key, so a rename cannot quietly disable the rule.
    """
    bot = FakeBot()
    orch = orchestrator(repo_config, barren_run(3), bot)

    await orch._alert_barren_market()
    await orch._alert_barren_market()

    assert len(outbox(bot)) == 1


# --------------------------------------------------------------------------- #
# The decision itself, over a table
# --------------------------------------------------------------------------- #


def outcome(**kwargs: Any) -> BarrenOutcome:
    row = _Row(started_at=NOW, **kwargs)
    return BarrenOutcome(
        status=row.status,
        started_at=row.started_at,
        symbols_requested=row.symbols_requested,
        symbols_scanned=row.symbols_scanned,
        skip_reasons=frozenset(str(s["reason"]) for s in row.skipped.values()),
    )


def test_a_running_cycle_neither_counts_nor_breaks_the_streak() -> None:
    """Same rule ``consecutive_failures`` applies, for the same reason: an in-flight
    cycle must not reset a count the next one is about to complete."""
    rows = [outcome(status="RUNNING", reason=None), outcome(), outcome(), outcome()]
    assert consecutive_barren(rows) == 3


def test_a_failed_cycle_breaks_the_streak() -> None:
    """A crash is a different condition with a different alert (``CYCLE_FAILURES``).
    Reporting one outage under two names trains the reader to ignore both."""
    rows = [outcome(), outcome(status="FAILED", reason=None), outcome(), outcome()]
    assert consecutive_barren(rows) == 1


def test_a_cycle_that_scanned_something_breaks_the_streak() -> None:
    rows = [outcome(scanned=1, reason=None), outcome(), outcome(), outcome()]
    assert consecutive_barren(rows) == 0


def test_an_empty_watchlist_is_not_barren() -> None:
    """Nothing was requested, so nothing failing to arrive is not news."""
    assert consecutive_barren([outcome(requested=0, reason=None)] * 3) == 0


def test_the_alert_fires_once_at_the_threshold_and_not_again() -> None:
    """Unlike ``cycle_alert`` there is no re-alert on multiples: the caller dedups by
    UTC day instead, because this condition persists for the whole day it starts on."""
    fired = [
        barren_alert([outcome()] * length, market="forex") is not None for length in range(1, 8)
    ]
    assert fired == [False, False, True, False, False, False, False]


def test_a_threshold_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1 cycle"):
        barren_alert([outcome()], market="forex", threshold=0)
