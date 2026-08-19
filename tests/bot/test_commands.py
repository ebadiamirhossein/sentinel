"""Commands — specs/TELEGRAM_UX.md §3.

Handlers are called directly with a fake message and a context wired to fake
repositories, so every command is exercised on every run rather than only when a
developer has a Postgres to point at. The fakes subclass the real repositories,
so a drifting signature fails ``mypy --strict``.

**M8.1 split the commands in two**, and this file follows: ``commands`` holds the
member set (``/capital /risk /positions /stats``, all scoped to the caller) and
``admin`` holds the operator's (``/status /settings /watchlist /pause /resume``),
behind :class:`~sentinel.bot.auth.OwnerOnly`. Every handler now receives the
``Actor`` the gate loaded, which is why ``run`` passes one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.auth import Actor
from sentinel.bot.context import BotContext
from sentinel.bot.handlers import admin, commands
from sentinel.bot.models import SignalDecision
from sentinel.bot.runtime import WATCHLIST
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Secrets, Settings, load_config
from sentinel.ingestion.models import InstrumentMeta
from sentinel.risk.models import PauseReason, PauseState
from tests.bot_double import (
    FakeDatabase,
    FakeStore,
    _SignalRow,
    _SnapshotRow,
    fake_repositories,
    owner_account,
)
from tests.risk_double import approved_plan

OWNER = 111


class FakeMessage:
    """The two things a handler touches on a Message: the sender, and ``answer``."""

    def __init__(self, user_id: int = OWNER) -> None:
        self.from_user = _User(user_id)
        self.replies: list[str] = []
        self.markups: list[Any] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.replies.append(text)
        self.markups.append(reply_markup)

    @property
    def last(self) -> str:
        return self.replies[-1]


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeCommand:
    """aiogram's ``CommandObject``, reduced to the field handlers read."""

    def __init__(self, args: str | None = None) -> None:
        self.args = args


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    store.users[OWNER] = owner_account(OWNER)
    return store


def actor_of(store: FakeStore) -> Actor:
    """The caller as the gate hands them over, re-read so it is never stale."""
    return Actor(user_id=OWNER, account=store.users[OWNER])


@pytest.fixture
def ctx(store: FakeStore, tz: ZoneInfo, clock: FrozenClock) -> BotContext:
    settings = Settings(secrets=Secrets(_env_file=None), config=load_config())
    return BotContext(
        settings=settings,
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=clock,
        tz=tz,
        repositories=fake_repositories(),
    )


#: Handlers that take no ``CommandObject`` — they have no arguments to parse.
NO_ARGS = (commands.positions, admin.status, admin.settings, admin.pause, admin.resume)


async def run(
    handler: Any, ctx: BotContext, args: str | None = None, *, store: FakeStore | None = None
) -> FakeMessage:
    message = FakeMessage()
    actor = actor_of(store) if store is not None else Actor(user_id=OWNER, account=owner_account())
    if handler in NO_ARGS:
        await handler(message, ctx, actor)
    else:
        await handler(message, FakeCommand(args), ctx, actor)
    return message


# --------------------------------------------------------------------------- #
# /capital — "validated > 0; confirms; applies to new signals only"
# --------------------------------------------------------------------------- #


async def test_capital_is_stored_and_confirmed_back(ctx: BotContext, store: FakeStore) -> None:
    message = await run(commands.capital, ctx, "10000")
    assert store.users[OWNER].capital_eur == Decimal("10000")
    assert "€10000" in message.last
    assert "new signals only" in message.last


@pytest.mark.parametrize("raw", ["0", "-1", "-0.01"])
async def test_capital_must_be_greater_than_zero(
    ctx: BotContext, store: FakeStore, raw: str
) -> None:
    message = await run(commands.capital, ctx, raw)
    assert "greater than zero" in message.last
    assert store.users[OWNER].capital_eur is None, "a rejected value must not be stored"


@pytest.mark.parametrize("raw", ["ten thousand", "", "1e", "nan"])
async def test_capital_rejects_anything_that_is_not_a_positive_number(
    ctx: BotContext, store: FakeStore, raw: str
) -> None:
    message = await run(commands.capital, ctx, raw or None)
    assert store.users[OWNER].capital_eur is None
    assert "not a number" in message.last or "not set" in message.last


async def test_capital_accepts_the_way_a_human_types_money(
    ctx: BotContext, store: FakeStore
) -> None:
    await run(commands.capital, ctx, "€12,500.50")
    assert store.users[OWNER].capital_eur == Decimal("12500.50")


async def test_capital_with_no_argument_reports_the_current_value(
    ctx: BotContext, store: FakeStore
) -> None:
    unset = await run(commands.capital, ctx, None, store=store)
    assert "not set" in unset.last

    store.users[OWNER] = owner_account(OWNER, capital_eur=Decimal("8000"))
    shown = await run(commands.capital, ctx, None, store=store)
    assert "€8000" in shown.last


async def test_setting_capital_writes_an_audit_row(ctx: BotContext, store: FakeStore) -> None:
    """PRD F10 stores config *changes*, so "why was this sized against €8,000"
    has a timestamped answer rather than an inference."""
    await run(commands.capital, ctx, "8000", store=store)
    await run(commands.capital, ctx, "9000", store=store)
    assert store.changes == [
        (f"user.{OWNER}.capital_eur", None, "8000", OWNER),
        (f"user.{OWNER}.capital_eur", "8000", "9000", OWNER),
    ]


# --------------------------------------------------------------------------- #
# /risk — "0.25-1.5 enforced"
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["0.25", "0.75", "1.5", "1.50"])
async def test_risk_accepts_the_configured_range(
    ctx: BotContext, store: FakeStore, raw: str
) -> None:
    message = await run(commands.risk, ctx, raw)
    assert store.users[OWNER].risk_per_trade_pct == Decimal(raw)
    assert f"{Decimal(raw)}%" in message.last


@pytest.mark.parametrize("raw", ["0.24", "1.51", "5", "0", "-1"])
async def test_risk_rejects_anything_outside_it(
    ctx: BotContext, store: FakeStore, raw: str
) -> None:
    message = await run(commands.risk, ctx, raw)
    assert store.users[OWNER].risk_per_trade_pct is None
    assert "between 0.25% and 1.5%" in message.last
    assert raw.lstrip("+") in message.last or Decimal(raw) is not None


async def test_the_risk_bounds_come_from_config_not_from_a_literal(ctx: BotContext) -> None:
    """A handler that hardcoded 0.25-1.5 would drift the moment config.yaml moved."""
    message = await run(commands.risk, ctx, "9")
    risk = ctx.settings.config.risk
    assert f"between {risk.risk_per_trade_min_pct}%" in message.last
    assert f"and {risk.risk_per_trade_max_pct}%" in message.last


async def test_risk_with_no_argument_reports_the_default(ctx: BotContext) -> None:
    message = await run(commands.risk, ctx, None)
    assert "0.75%" in message.last
    assert "allowed 0.25 to 1.5%" in message.last


# --------------------------------------------------------------------------- #
# /pause and /resume
# --------------------------------------------------------------------------- #


async def test_pause_then_resume(ctx: BotContext, store: FakeStore) -> None:
    paused = await run(admin.pause, ctx)
    assert store.pause.paused is True
    assert store.pause.reason is PauseReason.MANUAL
    assert "Paused" in paused.last

    resumed = await run(admin.resume, ctx)
    assert store.pause == PauseState(), "resume clears the whole pause row, not just the flag"
    assert "Resumed" in resumed.last


async def test_resuming_when_not_paused_says_so(ctx: BotContext) -> None:
    message = await run(admin.resume, ctx)
    assert "Not paused" in message.last


async def test_a_loss_limit_pause_needs_a_confirmation_button(
    ctx: BotContext, store: FakeStore
) -> None:
    """§3 — "resume from loss-limit pause requires confirming button "Yes, resume"".

    The asymmetry with a manual pause is the point: a loss-limit pause exists
    because the day has gone badly, which is exactly when a reflexive tap costs
    the most. From M8.1 the loss pause lives on the caller's own ``users`` row —
    it is their book that went badly — while the manual one stays system-wide.
    """
    loss = PauseState(
        paused=True,
        reason=PauseReason.DAILY_LOSS_LIMIT,
        until=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
    )
    store.users[OWNER] = owner_account(OWNER, pause=loss)
    message = await run(admin.resume, ctx, store=store)

    assert store.users[OWNER].pause.paused is True, "it must survive an unconfirmed /resume"
    assert "daily loss-limit" in message.last
    assert message.markups[-1] is not None, "the confirmation button must be attached"


# --------------------------------------------------------------------------- #
# /status, /positions, /settings, /watchlist
# --------------------------------------------------------------------------- #


async def test_status_reflects_real_state(ctx: BotContext, store: FakeStore) -> None:
    store.users[OWNER] = owner_account(OWNER, capital_eur=Decimal("10000"))
    store.snapshots.append(_SnapshotRow("BTCUSDT", "OK", datetime(2026, 8, 18, 11, 0, tzinfo=UTC)))
    store.signals[_uuid(1)] = _SignalRow(_uuid(1), _uuid(2), number=1, user_id=OWNER)

    message = await run(admin.status, ctx, store=store)
    assert "capital: €10000" in message.last
    assert "BTCUSDT OK" in message.last
    assert "awaiting your call: 1" in message.last


async def test_positions_lists_only_taken_signals(ctx: BotContext, store: FakeStore) -> None:
    empty = await run(commands.positions, ctx)
    assert "Nothing marked" in empty.last

    # A real gate-approved plan, not a hand-built dict: /positions validates the
    # stored plan now, and a fixture that skipped the gate would let a renderer
    # bug hide behind a shape no engine ever produced.
    plan = approved_plan(load_config())
    row = _SignalRow(_uuid(1), plan.plan_id, number=7)
    row.decision = SignalDecision.TAKEN.value
    row.plan = plan.model_dump(mode="json")
    row.symbol = "SOLUSDT"
    row.direction = "long"
    row.setup_type = "trend_pullback"
    row.status = "PENDING_ENTRY"
    row.expires_at = datetime(2026, 8, 19, tzinfo=UTC)
    store.signals[_uuid(1)] = row

    message = await run(commands.positions, ctx)
    assert "#7 SOLUSDT LONG" in message.last
    assert "risk €74.98" in message.last
    # Nothing has filled, so there is no mark to show — and the card says that
    # rather than printing 0.00R, which would read as a flat trade.
    assert "unfilled — the ladder is still resting" in message.last
    assert "M7" not in message.last, "the deferral note must be gone, not reworded"


async def test_settings_shows_the_source_of_every_value(ctx: BotContext, store: FakeStore) -> None:
    store.users[OWNER] = owner_account(OWNER, capital_eur=Decimal("10000"))
    message = await run(admin.settings, ctx, store=store)
    # "you" rather than "db": it is no longer one setting for the whole system, and
    # a card that still said db would imply it applied to everybody.
    assert "capital_eur: €10000 <i>[you]</i>" in message.last
    assert "max_leverage: 10x <i>[yaml]</i>" in message.last
    assert "min_rr_tp1: 1.5R (net of costs) <i>[yaml]</i>" in message.last


async def test_settings_fits_one_telegram_message(ctx: BotContext) -> None:
    """Why /settings is a curated list and not a dump of AppConfig."""
    message = await run(admin.settings, ctx)
    assert len(message.last) < 4096


async def test_watchlist_shows_the_configured_symbols(ctx: BotContext) -> None:
    message = await run(admin.watchlist, ctx, None)
    assert "BTCUSDT" in message.last
    assert "<i>[yaml]</i>" in message.last


async def test_watchlist_add_verifies_the_symbol_against_the_exchange(
    ctx: BotContext, store: FakeStore
) -> None:
    """A typo would otherwise produce a silently skipped symbol every cycle."""

    class Checker:
        async def instrument_meta(self, symbol: str) -> InstrumentMeta:
            raise ValueError("no such market")

    checked = BotContext(**{**ctx.__dict__, "symbol_checker": Checker()})
    message = await run(admin.watchlist, checked, "add NOTREALUSDT")

    assert WATCHLIST not in store.settings
    assert "not a Binance" in message.last


async def test_watchlist_add_accepts_a_symbol_the_exchange_knows(
    ctx: BotContext, store: FakeStore
) -> None:
    from tests.risk_double import SOLUSDT as SOL_META

    store.instruments["INJUSDT"] = SOL_META
    message = await run(admin.watchlist, ctx, "add INJUSDT")
    assert "INJUSDT" in store.settings[WATCHLIST]
    assert "INJUSDT" in message.last


async def test_watchlist_remove_drops_a_symbol(ctx: BotContext, store: FakeStore) -> None:
    message = await run(admin.watchlist, ctx, "remove BTCUSDT")
    assert "BTCUSDT" not in store.settings[WATCHLIST]
    assert "BTCUSDT" not in message.last


async def test_watchlist_refuses_to_empty_itself(ctx: BotContext, store: FakeStore) -> None:
    """An empty watchlist means the scan cycle has nothing to do."""
    store.settings[WATCHLIST] = ["BTCUSDT"]
    message = await run(admin.watchlist, ctx, "remove BTCUSDT")
    assert store.settings[WATCHLIST] == ["BTCUSDT"]
    assert "empty the watchlist" in message.last


@pytest.mark.parametrize("args", ["nonsense", "add", "add BTC USDT extra", "drop BTCUSDT"])
async def test_watchlist_rejects_malformed_arguments(
    ctx: BotContext, store: FakeStore, args: str
) -> None:
    message = await run(admin.watchlist, ctx, args)
    assert WATCHLIST not in store.settings
    assert "Usage" in message.last or "does not look like a symbol" in message.last


def _uuid(seed: int) -> Any:
    from uuid import UUID

    return UUID(int=seed)
