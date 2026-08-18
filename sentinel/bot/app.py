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

from sentinel.bot.allowlist import AllowlistMiddleware
from sentinel.bot.context import BotContext, Repositories
from sentinel.bot.formatting import zone_info
from sentinel.bot.handlers import callbacks_router, commands_router
from sentinel.bot.runtime import SymbolChecker
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import Settings
from sentinel.core.logging import get_logger
from sentinel.storage.db import Database

log = get_logger(__name__)


def build_context(
    settings: Settings,
    database: Database,
    *,
    clock: Clock | None = None,
    symbol_checker: SymbolChecker | None = None,
    repositories: Repositories | None = None,
) -> BotContext:
    return BotContext(
        settings=settings,
        database=database,
        clock=clock or SystemClock(),
        tz=zone_info(settings.config.telegram.owner_timezone),
        symbol_checker=symbol_checker,
        repositories=repositories or Repositories(),
    )


def build_dispatcher(ctx: BotContext) -> Dispatcher:
    """Routers, the allowlist, and the context every handler receives.

    The allowlist is attached to messages **and** callback queries: a card
    forwarded to a stranger carries its buttons with it, and a button is a
    perfectly good way to try to write to someone else's database.
    """
    dispatcher = Dispatcher()
    dispatcher["ctx"] = ctx

    allowlist = AllowlistMiddleware(ctx.settings.secrets.allowed_user_ids)
    dispatcher.message.middleware(allowlist)
    dispatcher.callback_query.middleware(allowlist)

    dispatcher.include_router(commands_router)
    dispatcher.include_router(callbacks_router)
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

    def __init__(self, bot: Bot, dispatcher: Dispatcher) -> None:
        self.bot = bot
        self.dispatcher = dispatcher
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self._task is not None:  # pragma: no cover — start is called once
            return
        # Updates queued while the process was down are dropped: they are almost
        # certainly button presses on cards that have since been answered, and
        # replaying them would rewrite decisions the owner already made.
        await self.bot.delete_webhook(drop_pending_updates=True)
        self._task = asyncio.create_task(self.dispatcher.start_polling(self.bot))
        log.info("bot.polling_started")

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
    return BotRunner(build_bot(settings), build_dispatcher(ctx))


__all__ = ["BotRunner", "build_bot", "build_context", "build_dispatcher", "build_runner"]
