"""User-id allowlist — specs/TELEGRAM_UX.md §1: "all other users get silence".

Silence is literal and deliberate. An "unauthorized" reply would confirm to a
stranger that they found a live private bot, and this one answers questions about
someone's capital, open positions and pause state. Every rejected update is
logged, because a stream of them is worth knowing about, but nothing is sent.

The middleware is registered on both the message and the callback-query observers:
an inline button is a perfectly good way to try to write to someone else's
database, and a card forwarded to a stranger carries its buttons with it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from sentinel.core.logging import get_logger

log = get_logger(__name__)


class AllowlistMiddleware(BaseMiddleware):
    """Drop every update whose sender is not on the allowlist.

    An **empty** allowlist means nobody passes, not everybody: an unconfigured
    ``TELEGRAM_ALLOWED_USER_IDS`` is a misconfiguration, and failing open would
    hand the bot to whoever finds it first.
    """

    def __init__(self, allowed_user_ids: tuple[int, ...]) -> None:
        self._allowed = frozenset(allowed_user_ids)
        if not self._allowed:
            log.warning("bot.allowlist_empty", detail="no user may interact with this bot")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id not in self._allowed:
            log.warning(
                "bot.rejected_update",
                user_id=None if user is None else user.id,
                update_type=type(event).__name__,
                text=_preview(event),
            )
            return None
        return await handler(event, data)


def _preview(event: TelegramObject) -> str | None:
    """What was attempted, truncated — useful in a log, never echoed back."""
    if isinstance(event, Message):
        return None if event.text is None else event.text[:64]
    if isinstance(event, CallbackQuery):
        return None if event.data is None else event.data[:64]
    return None


__all__ = ["AllowlistMiddleware"]
