"""The Bot surface a handler is allowed to touch (M8.1).

``SupportsSending`` (messages, for the publisher, the notifier and the alerter) and
``SupportsCommands`` (the ``/`` menu) are deliberately separate protocols: three
components need the first and none of them needs the second.

A handler that approves a user needs both — it messages the new member *and* gives
their chat a menu — so the two are composed here rather than widened into each
other. Keeping the composition in one place means a fourth component still declares
only what it uses.
"""

from __future__ import annotations

from typing import Protocol

from sentinel.bot.menu import SupportsCommands
from sentinel.bot.publisher import SupportsSending


class SupportsBot(SupportsSending, SupportsCommands, Protocol):
    """Sends messages and publishes command menus — what aiogram injects as ``bot``."""


__all__ = ["SupportsBot"]
