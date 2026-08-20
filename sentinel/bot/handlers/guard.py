"""A handler that fails must say so — M8.2 §1's lesson, one layer in (M8.6).

journal/M8_2_REPORT.md found every owner command silent for a week because a
``TypeError`` inside a filter went to aiogram's error middleware and was logged as
"update is not handled". The finding that mattered was not the ``TypeError``:

> When a component's failure mode is *silence*, the absence of a complaint is not
> evidence — only a positive test is.

M8.6 hit the same shape from the other side. ``/snapshot ADAUSDT`` built its card
perfectly and the **send** raised ``TelegramBadRequest`` — an unescaped ``<`` in the
EMA-stack label — which aiogram caught, logged, and turned into nothing at all. On a
bot where silence is a *designed* response to anyone without standing
(``bot/auth.py``), a caller cannot tell a crash from a command that does not exist.

So every handler on the member router answers, even when it fails. This does not
make the failure less serious; it makes it visible to the one person positioned to
report it, and it costs one line per handler.

**Deliberately a decorator rather than a router-level error handler.** ``Router.errors``
would catch everything propagating through the dispatcher, including anything raised
inside ``AuthMiddleware`` itself — and replying there would answer a stranger whose
whole guarantee is that they hear nothing. This wraps named handlers that already sit
behind the gate, and can reach nothing else.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, cast

from sentinel.core.logging import get_logger

log = get_logger(__name__)

#: Deliberately vague about the cause and specific about what to do next. A caller
#: cannot act on a traceback, and this bot must never invite one to retry a command
#: that just changed something — so the wording claims nothing about whether the
#: work was done, only that the failure is recorded.
FAILED = (
    "⚠️ Something went wrong handling that.\n\n"
    "The failure is in the server log with a full traceback. Try again — and if it "
    "keeps happening, the log will say why."
)

type Handler = Callable[..., Awaitable[Any]]


def answers_on_failure[H: Handler](handler: H) -> H:
    """Reply instead of dying quietly. Re-raises nothing, logs everything.

    ``functools.wraps`` matters here beyond tidiness: aiogram builds a handler's
    injected arguments from ``inspect.unwrap(callback)``, so the wrapper must carry
    ``__wrapped__`` or the handler would stop receiving ``ctx``, ``actor`` and
    ``command`` — a decorator that silently changed dependency injection would be a
    far worse bug than the one it is here to fix.
    """

    @wraps(handler)
    async def guarded(message: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(message, *args, **kwargs)
        except Exception:
            # `exc_info` rather than a formatted string: the traceback is the whole
            # value of this log line, and it is the only record of what happened.
            log.error("bot.handler_failed", handler=handler.__name__, exc_info=True)
            try:
                await message.answer(FAILED)
            except Exception:
                # Telegram itself is unreachable, or the very thing that failed was
                # the send. Nothing left to try, and raising here would put the
                # original failure out of reach behind a second one.
                log.warning("bot.failure_notice_undeliverable", handler=handler.__name__)
            return None

    return cast(H, guarded)


__all__ = ["FAILED", "answers_on_failure"]
