"""aiogram 3 Telegram bot: signal cards, buttons, commands (M6).

specs/TELEGRAM_UX.md. The one rule that shapes everything here: **the bot renders,
it never computes.** Every number on a card is a field of ``TradePlan``.

This package re-exports ``models`` only, and deliberately not ``publisher`` or the
handlers. ``storage/repositories.py`` translates ``SignalRecord`` into rows and so
imports ``sentinel.bot.models``; if importing that module also dragged in the
publisher — which imports ``storage.repositories`` — the two would deadlock on a
partially initialized module, and it would fail first for whoever imported
storage first (Alembic did). ``sentinel.bot.models`` depends on nothing but
``risk.models``, which keeps CLAUDE.md's direction intact: bot → storage, never
back. Import the rest explicitly, as ``storage/__init__.py`` also requires.
"""

from sentinel.bot.models import (
    MessageKind,
    MessageStatus,
    PostedMessage,
    SignalDecision,
    SignalRecord,
    SignalStatus,
)

__all__ = [
    "MessageKind",
    "MessageStatus",
    "PostedMessage",
    "SignalDecision",
    "SignalRecord",
    "SignalStatus",
]
