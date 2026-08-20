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

from typing import Any, Protocol

from sentinel.bot.menu import SupportsCommands
from sentinel.bot.publisher import SupportsSending


class SupportsDocuments(Protocol):
    """Sending a file (M8.6). Narrow, and separate, for the reason above.

    ``/journal`` is the only thing in this system that produces a document, and the
    publisher, the notifier and the alerter — the three components that depend on
    ``SupportsSending`` — will never send one. Adding ``send_document`` there would
    make all three declare a capability none of them has any use for.

    ``document`` is ``Any`` for the same reason ``send_media_group``'s ``media`` is:
    a real ``aiogram.Bot`` takes ``InputFile | str``, and naming the concrete
    ``BufferedInputFile`` the handler happens to pass would make the real Bot fail
    the protocol rather than satisfy it.
    """

    async def send_document(
        self, *, chat_id: int, document: Any, caption: str, parse_mode: str
    ) -> Any: ...


class SupportsBot(SupportsSending, SupportsCommands, SupportsDocuments, Protocol):
    """Messages, command menus and documents — what aiogram injects as ``bot``."""


__all__ = ["SupportsBot", "SupportsDocuments"]
