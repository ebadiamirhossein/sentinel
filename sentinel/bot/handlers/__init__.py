"""aiogram routers, in the order ``build_dispatcher`` includes them.

``membership`` first (§7's join/leave, reachable without full standing), then the
member commands (§3), then ``admin`` behind the owner filter, then the inline-button
callbacks (§2), and ``replies`` last because it matches any reply and must not shadow
a command that happens to be sent as one.
"""

from sentinel.bot.handlers.admin import admin_router
from sentinel.bot.handlers.callbacks import callbacks_router
from sentinel.bot.handlers.commands import commands_router
from sentinel.bot.handlers.membership import membership_router
from sentinel.bot.handlers.replies import replies_router

__all__ = [
    "admin_router",
    "callbacks_router",
    "commands_router",
    "membership_router",
    "replies_router",
]
