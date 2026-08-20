"""``answers_on_failure`` — a handler that breaks must say so (M8.6).

journal/M8_2_REPORT.md §1's lesson, arrived at a second time from the other side.
There, a ``TypeError`` in a filter made nine owner commands silent for a week; here,
a ``TelegramBadRequest`` from an unescaped ``<`` made ``/snapshot`` silent for every
symbol. Both times the mechanism was the same: aiogram catches what a handler raises,
logs it, and returns nothing — and on a bot where **silence is the designed response
to anyone without standing**, that is indistinguishable from a command not existing.

The tests here are about the property, not the incident: it answers, it says nothing
misleading, it survives a broken reply path, and — the one that matters most — it
does not quietly change what a working handler does.
"""

from __future__ import annotations

from typing import Any

import pytest

from sentinel.bot.handlers.guard import FAILED, answers_on_failure


class FakeMessage:
    def __init__(self, *, answer_fails: bool = False) -> None:
        self.replies: list[str] = []
        self._answer_fails = answer_fails

    async def answer(self, text: str, **kwargs: Any) -> None:
        if self._answer_fails:
            raise RuntimeError("Telegram is unreachable")
        self.replies.append(text)


async def test_a_working_handler_is_untouched() -> None:
    """The decorator has to be invisible when nothing goes wrong — it wraps every
    member command, and a guard that changed behaviour on the happy path would be a
    worse bug than the one it prevents."""

    @answers_on_failure
    async def handler(message: FakeMessage, *, ctx: str) -> str:
        await message.answer(f"the real answer, {ctx}")
        return "returned"

    message = FakeMessage()
    assert await handler(message, ctx="ok") == "returned"
    assert message.replies == ["the real answer, ok"]


async def test_a_failing_handler_answers_instead_of_going_quiet() -> None:
    @answers_on_failure
    async def handler(message: FakeMessage) -> None:
        raise ValueError("the card would not render")

    message = FakeMessage()
    # Awaiting without `pytest.raises` *is* the assertion that nothing escaped to
    # aiogram — which is where an escaped exception becomes "update is not handled".
    await handler(message)
    assert message.replies == [FAILED]


def test_the_apology_claims_nothing_about_whether_the_work_was_done() -> None:
    """It wraps ``/capital`` and ``/risk``, which **write**. A message saying
    "nothing was changed" would be a guess, and on the two settings that decide a
    position size it would be the wrong kind of guess."""
    assert "nothing was changed" not in FAILED.lower()
    assert "log" in FAILED.lower(), "it has to say where the cause actually is"


async def test_a_failure_to_deliver_the_apology_is_not_a_second_exception() -> None:
    """The realistic case is that the *send* is what failed — which is exactly what
    happened in production. Raising from the except block would replace the original
    failure with a less informative one and put the traceback out of reach."""

    @answers_on_failure
    async def handler(message: FakeMessage) -> None:
        raise ValueError("the send failed")

    await handler(FakeMessage(answer_fails=True))


async def test_it_does_not_swallow_cancellation() -> None:
    """``CancelledError`` is a ``BaseException`` and must pass through: swallowing it
    would make a shutdown hang on a handler that refuses to stop."""

    @answers_on_failure
    async def handler(message: FakeMessage) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await handler(FakeMessage())


def test_the_wrapper_keeps_the_signature_aiogram_injects_from() -> None:
    """The one way this decorator could break everything it touches.

    aiogram builds a handler's arguments from ``inspect.unwrap(callback)``, so a
    wrapper without ``__wrapped__`` would silently stop ``ctx``, ``actor`` and
    ``command`` being passed — and the guard would then catch the resulting
    ``TypeError`` and answer, making a completely broken router look merely
    apologetic. ``tests/bot/test_dispatcher_wiring.py`` asserts the same property
    end-to-end; this states it directly.
    """
    import inspect

    async def handler(message: Any, ctx: Any, actor: Any) -> None: ...

    guarded = answers_on_failure(handler)
    assert inspect.unwrap(guarded) is handler
    assert list(inspect.signature(inspect.unwrap(guarded)).parameters) == [
        "message",
        "ctx",
        "actor",
    ]
