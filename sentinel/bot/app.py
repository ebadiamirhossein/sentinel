"""Assembling and running the bot (ARCHITECTURE.md §2 — one process, one loop).

The bot lives inside the app's existing event loop, started from the FastAPI
lifespan next to the scheduler. It is not a second entrypoint: a separate process
would need its own database pool, its own config reload and its own restart
policy, for a component whose whole job is to read the same Postgres.

Long polling rather than a webhook — a webhook needs an inbound port and a
certificate on the VPS, which ARCHITECTURE.md §5's deployment does not have.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from sentinel.analyst.persian.summariser import PersianSummariser
from sentinel.bot.auth import AuthMiddleware
from sentinel.bot.context import BotContext, Repositories
from sentinel.bot.formatting import zone_info
from sentinel.bot.handlers import (
    admin_router,
    callbacks_router,
    commands_router,
    membership_router,
    persian_router,
    replies_router,
)
from sentinel.bot.menu import publish_menu
from sentinel.bot.models import UserRole
from sentinel.bot.runtime import SymbolChecker
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import Settings
from sentinel.core.logging import get_logger
from sentinel.llm.client import AnthropicClient
from sentinel.storage.db import Database

log = get_logger(__name__)


def build_summariser(settings: Settings) -> PersianSummariser | None:
    """The 🇮🇷 فارسی button's model client, or ``None`` if it cannot have one.

    One client for the process rather than one per press: a press is rare, but a new
    HTTP client per press would leak a connection pool every time. It is never closed,
    which is correct for a component whose lifetime is the process's.

    Returns ``None`` when the feature is disabled or no API key is configured, and the
    handler degrades explicitly rather than raising on the first press — the same
    posture ``symbol_checker`` takes one field over.
    """
    key = settings.secrets.anthropic_api_key
    if not settings.config.persian_summary.enabled or key is None:
        log.info(
            "bot.persian_disabled",
            enabled=settings.config.persian_summary.enabled,
            has_key=key is not None,
        )
        return None
    return PersianSummariser(
        AnthropicClient(settings.config.llm, api_key=key.get_secret_value()),
        settings.config,
    )


def build_context(
    settings: Settings,
    database: Database,
    *,
    clock: Clock | None = None,
    symbol_checker: SymbolChecker | None = None,
    repositories: Repositories | None = None,
    summariser: PersianSummariser | None = None,
) -> BotContext:
    return BotContext(
        settings=settings,
        database=database,
        clock=clock or SystemClock(),
        tz=zone_info(settings.config.telegram.owner_timezone),
        symbol_checker=symbol_checker,
        repositories=repositories or Repositories(),
        summariser=summariser or build_summariser(settings),
    )


def build_dispatcher(ctx: BotContext) -> Dispatcher:
    """Routers, the authorization gate, and the context every handler receives.

    The gate is attached to messages **and** callback queries: a card forwarded to a
    stranger carries its buttons with it, and a button is a perfectly good way to try
    to write to someone else's database.

    Router order is load-bearing. ``membership`` first, because ``/start`` and
    ``/help`` are the only things a caller without full standing may reach.
    ``admin`` after the member commands, so an owner-only name never shadows one
    everybody has. ``replies`` last, because it matches any reply and must not shadow
    a command that happens to be sent as one.
    """
    dispatcher = Dispatcher()
    dispatcher["ctx"] = ctx

    # **Outer**, not inner, and this is load-bearing rather than stylistic.
    #
    # aiogram resolves a sub-router's *root* filters inside `Router._propagate_event`,
    # before that router's handlers — and therefore before any inner middleware, which
    # only wraps a handler once one has matched. `admin_router` carries `OwnerOnly()`
    # as a root filter, and `OwnerOnly` takes `actor` — which this gate is what injects.
    # Registered as inner, the filter was evaluated with `actor` absent and aiogram
    # raised `TypeError: OwnerOnly.__call__() missing 1 required positional argument`,
    # which the dispatcher's error middleware swallows into "update is not handled".
    # Every owner command answered with silence, and silence is what OwnerOnly is
    # *supposed* to produce for a non-owner — so a dead admin surface was
    # indistinguishable from working access control (journal/M8_2_REPORT.md §1).
    #
    # Outer middleware runs in `Router.propagate_event` before propagation descends,
    # so `actor` is in `data` by the time any sub-router's root filters are checked.
    # It also matches what `auth.classify` already documents: the gate runs *before*
    # routing, so an unauthorized update never reaches a router at all.
    gate = AuthMiddleware(ctx)
    dispatcher.message.outer_middleware(gate)
    dispatcher.callback_query.outer_middleware(gate)

    dispatcher.include_router(membership_router)
    dispatcher.include_router(commands_router)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(callbacks_router)
    # M11p. Before ``replies``, which matches any reply and must not shadow it, and
    # after ``callbacks``, which owns the decision buttons on the same cards.
    dispatcher.include_router(persian_router)
    dispatcher.include_router(replies_router)
    return dispatcher


def build_bot(settings: Settings) -> Bot:
    token = settings.secrets.telegram_bot_token
    if token is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set — see .env.example")
    return Bot(
        token=token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=settings.config.telegram.parse_mode),
    )


class BotRunner:
    """Owns the polling task; started and stopped by the app lifespan."""

    def __init__(self, bot: Bot, dispatcher: Dispatcher, ctx: BotContext) -> None:
        self.bot = bot
        self.dispatcher = dispatcher
        self.ctx = ctx
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self._task is not None:  # pragma: no cover — start is called once
            return
        # Updates queued while the process was down are dropped: they are almost
        # certainly button presses on cards that have since been answered, and
        # replaying them would rewrite decisions the owner already made.
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.publish_menu()
        self._task = asyncio.create_task(self.dispatcher.start_polling(self.bot))
        log.info("bot.polling_started")

    async def publish_menu(self) -> None:
        """Register the ``/`` menu for every scope (M8.1).

        Done at every start rather than once at approval, because the command set is
        code: a release that adds a command must reach the menus of people who were
        approved before it existed. ``publish_menu`` swallows its own failures — the
        menu is a convenience and the signals are the product.
        """
        async with self.ctx.database.session() as session:
            accounts = await self.ctx.repositories.users(session).approved()
        owner = next((a for a in accounts if a.role is UserRole.OWNER), None)
        await publish_menu(
            self.bot,
            owner_id=None if owner is None else owner.telegram_user_id,
            member_ids=[a.telegram_user_id for a in accounts],
        )

    async def stop(self) -> None:
        if self._task is None:  # pragma: no cover — stop mirrors start
            return
        await self.dispatcher.stop_polling()
        self._task.cancel()
        try:
            await self._task
        except Exception:
            # Shutdown must not raise: a bot that fails to stop cleanly would take
            # the whole app's lifespan down with it, and the polling task is
            # cancelled either way. CancelledError is a BaseException and passes
            # through, which is what asyncio wants during cancellation.
            log.warning("bot.polling_stop_error", exc_info=True)
        finally:
            self._task = None
            await self.bot.session.close()
        log.info("bot.polling_stopped")


def build_runner(
    settings: Settings,
    database: Database,
    *,
    clock: Clock | None = None,
    symbol_checker: SymbolChecker | None = None,
) -> BotRunner:
    ctx = build_context(settings, database, clock=clock, symbol_checker=symbol_checker)
    return BotRunner(build_bot(settings), build_dispatcher(ctx), ctx)


__all__ = ["BotRunner", "build_bot", "build_context", "build_dispatcher", "build_runner"]
