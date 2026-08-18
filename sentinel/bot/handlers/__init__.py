"""aiogram routers: commands (§3), inline-button callbacks and prompt replies (§2)."""

from sentinel.bot.handlers.callbacks import callbacks_router
from sentinel.bot.handlers.commands import commands_router
from sentinel.bot.handlers.replies import replies_router

__all__ = ["callbacks_router", "commands_router", "replies_router"]
