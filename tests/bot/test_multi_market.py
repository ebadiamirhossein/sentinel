"""What the Telegram surfaces do once a second market is enabled (M10a Step 7).

Two halves, and the second is the one that protects the live system.

**Enabled:** every surface names its market and never merges two of them into one
figure. **Disabled:** every surface renders exactly what it rendered before this
milestone — which ``tests/golden`` pins byte for byte, and which this file states as
the rule the renderers actually follow (``section_header`` returns ``""``).

Forex has no adapter, no screener and no rows in M10a. These tests enable it in
*config only*, which is precisely the state the shape has to be reviewable in before
M10b fills it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.auth import Actor
from sentinel.bot.cards import signal_card
from sentinel.bot.context import BotContext
from sentinel.bot.handlers import admin, commands
from sentinel.bot.markets import market_of_symbol, resolve_markets, section_header
from sentinel.bot.models import SignalRecord
from sentinel.bot.runtime import Invalid, watchlist_key
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings
from sentinel.core.markets import Market
from sentinel.risk.models import TradePlan
from tests.bot.telegram_html import assert_sendable
from tests.bot_double import OWNER_ID, FakeDatabase, FakeStore, fake_repositories, owner_account
from tests.risk_double import approved_plan

OWNER = OWNER_ID
TZ = ZoneInfo("Europe/Vilnius")
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def enabled_forex(config: AppConfig) -> AppConfig:
    """Forex switched on. A no-op on the shipped config since M10d, kept because
    several tests build a single-market one and want the other form."""
    forex = config.market(Market.FOREX).model_copy(update={"enabled": True})
    return config.model_copy(update={"markets": {**config.markets, Market.FOREX: forex}})


def disabled_forex(config: AppConfig) -> AppConfig:
    """Forex switched off — what the shipped config was until M10d.

    The three tests below are about **a disabled market**, not about which market
    happens to be disabled this month, so they now say so explicitly rather than
    leaning on the shipped flag. Leaning on it is why they broke on switch-on day, and
    the version that reads the shipped config was only ever testing the config.
    """
    forex = config.market(Market.FOREX).model_copy(update={"enabled": False})
    return config.model_copy(update={"markets": {**config.markets, Market.FOREX: forex}})


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    store.users[OWNER] = owner_account(OWNER, capital_eur=Decimal("10000"))
    return store


def context(store: FakeStore, config: AppConfig) -> BotContext:
    return BotContext(
        settings=Settings(secrets=Secrets(_env_file=None), config=config),
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        tz=TZ,
        repositories=fake_repositories(),
    )


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def answer(self, text: str, **_: Any) -> None:
        self.replies.append(text)

    @property
    def last(self) -> str:
        return self.replies[-1]


class FakeCommand:
    def __init__(self, args: str | None = None) -> None:
        self.args = args


#: ``/positions`` is the one surface here that parses no argument — it always
#: covers every enabled market, because there is nothing to narrow it by.
NO_ARGS = (commands.positions,)


async def run(handler: Any, ctx: BotContext, args: str | None = None) -> FakeMessage:
    message = FakeMessage()
    actor = Actor(user_id=OWNER, account=owner_account(OWNER))
    if handler in NO_ARGS:
        await handler(message, ctx, actor)
    else:
        await handler(message, FakeCommand(args), ctx, actor)
    return message


# --------------------------------------------------------------------------- #
# The condition every market tag hangs off
# --------------------------------------------------------------------------- #


def test_the_header_is_empty_with_one_market(repo_config: AppConfig) -> None:
    """The whole "crypto output does not change" promise, in one function.

    Every surface asks ``section_header``; with one market it answers with nothing,
    and there is exactly one place for that to be wrong. specs/TELEGRAM_UX.md §3e
    keeps promising it after switch-on, so it keeps being asserted after switch-on —
    against an explicitly single-market config rather than against the shipped one.
    """
    single = disabled_forex(repo_config)

    assert section_header(Market.CRYPTO, single) == ""
    assert single.multi_market is False


def test_the_header_names_the_market_once_two_are_enabled(repo_config: AppConfig) -> None:
    config = enabled_forex(repo_config)

    assert config.multi_market is True
    assert section_header(Market.CRYPTO, config) == "<b>— CRYPTO —</b>"
    assert section_header(Market.FOREX, config) == "<b>— FOREX —</b>"


# --------------------------------------------------------------------------- #
# The signal card
# --------------------------------------------------------------------------- #


def test_the_card_carries_no_market_tag_by_default(bot_config: AppConfig) -> None:
    """Default off, so a caller that forgets cannot change a live crypto card."""
    record = SignalRecord(plan=approved_plan(bot_config), user_id=OWNER, number=1)

    assert "CRYPTO" not in signal_card(record, TZ)


def test_the_card_tags_the_market_when_asked(bot_config: AppConfig) -> None:
    plan: TradePlan = approved_plan(bot_config)
    record = SignalRecord(plan=plan, user_id=OWNER, number=1, market=Market.FOREX)

    card = signal_card(record, TZ, show_market=True)

    assert "FOREX · " in card
    assert plan.symbol in card
    assert_sendable(card)


def test_the_tag_is_the_only_difference(bot_config: AppConfig) -> None:
    """Not "roughly the same": the tagged and untagged cards differ by the tag alone.

    A renderer that also reflowed a line, or moved the symbol, would pass a
    substring check and fail the golden. This states the rule the golden enforces.
    """
    record = SignalRecord(plan=approved_plan(bot_config), user_id=OWNER, number=1)

    tagged = signal_card(record, TZ, show_market=True)

    assert tagged.replace("CRYPTO · ", "", 1) == signal_card(record, TZ)


# --------------------------------------------------------------------------- #
# Resolving which markets a command covers
# --------------------------------------------------------------------------- #


def test_no_argument_means_every_enabled_market(repo_config: AppConfig) -> None:
    """The documented default (specs/TELEGRAM_UX.md §3e).

    Not "the first one": a reader shown one market's numbers with nothing saying the
    other exists has been told something false by omission.
    """
    assert resolve_markets(None, enabled_forex(repo_config)) == (Market.CRYPTO, Market.FOREX)


def test_a_named_market_narrows(repo_config: AppConfig) -> None:
    assert resolve_markets("forex", enabled_forex(repo_config)) == (Market.FOREX,)


def test_a_disabled_market_is_an_error_not_an_empty_answer(repo_config: AppConfig) -> None:
    """Somebody asking for forex today is told it is switched off, rather than
    handed a blank card they would read as "no signals yet"."""
    refused = resolve_markets("forex", disabled_forex(repo_config))

    assert isinstance(refused, Invalid)
    assert "not enabled" in refused.message


def test_an_argument_that_is_not_a_market_falls_through(repo_config: AppConfig) -> None:
    """``/stats 90d`` is a window, not a market: it narrows nothing and refuses
    nothing, so the command covers every enabled market over that window."""
    config = enabled_forex(repo_config)

    assert resolve_markets("90d", config) == (Market.CRYPTO, Market.FOREX)


def test_a_symbol_names_its_own_market(repo_config: AppConfig) -> None:
    config = enabled_forex(repo_config)

    assert market_of_symbol("SOLUSDT", config) is Market.CRYPTO
    assert market_of_symbol("EURUSD", config) is Market.FOREX
    # Unknown: the caller is about to render "not on the watchlist", whose whole
    # content is that there is no data, so the first enabled market is the answer.
    assert market_of_symbol("NOPEUSDT", config) is Market.CRYPTO


# --------------------------------------------------------------------------- #
# The surfaces themselves, reached through their real handlers
# --------------------------------------------------------------------------- #


async def test_watchlist_lists_every_market(store: FakeStore, repo_config: AppConfig) -> None:
    message = await run(admin.watchlist, context(store, enabled_forex(repo_config)))

    assert "— CRYPTO —" in message.last
    assert "— FOREX —" in message.last
    assert "EURUSD" in message.last and "BTCUSDT" in message.last
    assert_sendable(message.last)


async def test_watchlist_edits_target_the_named_market(
    store: FakeStore, repo_config: AppConfig
) -> None:
    ctx = context(store, enabled_forex(repo_config))

    await run(admin.watchlist, ctx, "forex remove USDJPY")

    assert store.settings[watchlist_key(Market.FOREX)] == ["EURUSD", "GBPUSD"]
    assert watchlist_key(Market.CRYPTO) not in store.settings, "crypto is untouched"


async def test_stats_renders_one_block_per_market_and_never_a_total(
    store: FakeStore, repo_config: AppConfig
) -> None:
    """Two headers, two ``📊 Stats`` blocks, and no combined figure anywhere.

    There is no assertion available for "no total" beyond counting the blocks,
    because :class:`~sentinel.stats.models.StatsReport` has no field that could hold
    one — which is the point of Step 6 being a type change rather than a rule.
    """
    message = await run(commands.stats, context(store, enabled_forex(repo_config)))

    assert message.last.count("📊 <b>Stats</b>") == 2
    assert "— CRYPTO —" in message.last and "— FOREX —" in message.last
    assert_sendable(message.last)


async def test_stats_can_be_narrowed_to_one_market(
    store: FakeStore, repo_config: AppConfig
) -> None:
    message = await run(commands.stats, context(store, enabled_forex(repo_config)), "forex")

    assert message.last.count("📊 <b>Stats</b>") == 1
    assert "— FOREX —" in message.last


async def test_positions_renders_a_block_per_market(
    store: FakeStore, repo_config: AppConfig
) -> None:
    message = await run(commands.positions, context(store, enabled_forex(repo_config)))

    assert message.last.count("Nothing marked ✅ Taken yet.") == 2
    assert "— FOREX —" in message.last
    assert_sendable(message.last)


async def test_pulse_reports_each_markets_last_cycle(
    store: FakeStore, repo_config: AppConfig
) -> None:
    message = await run(commands.pulse, context(store, enabled_forex(repo_config)))

    assert message.last.count("📡 <b>Pulse</b>") == 2
    assert_sendable(message.last)


async def test_pulse_can_be_narrowed_to_one_market(
    store: FakeStore, repo_config: AppConfig
) -> None:
    message = await run(commands.pulse, context(store, enabled_forex(repo_config)), "forex")

    assert message.last.count("📡 <b>Pulse</b>") == 1
    assert "— FOREX —" in message.last


async def test_pulse_refuses_a_disabled_market(store: FakeStore, repo_config: AppConfig) -> None:
    message = await run(commands.pulse, context(store, disabled_forex(repo_config)), "forex")

    assert "not enabled" in message.last
    assert_sendable(message.last)


async def test_status_can_be_narrowed_to_one_market(
    store: FakeStore, repo_config: AppConfig
) -> None:
    message = await run(admin.status, context(store, enabled_forex(repo_config)), "forex")

    assert "— FOREX —" in message.last
    # Three symbols, not ten: the card is showing forex's watchlist, not crypto's.
    assert "watchlist: 3 symbols" in message.last
    assert_sendable(message.last)


async def test_status_defaults_to_the_first_enabled_market(
    store: FakeStore, repo_config: AppConfig
) -> None:
    message = await run(admin.status, context(store, enabled_forex(repo_config)))

    assert "watchlist: 10 symbols" in message.last
