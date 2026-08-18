"""aiogram routers: commands (§3) and inline-button callbacks (§2)."""

from sentinel.bot.handlers.callbacks import callbacks_router
from sentinel.bot.handlers.commands import commands_router

__all__ = ["callbacks_router", "commands_router"]
