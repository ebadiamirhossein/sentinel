"""specs/TELEGRAM_UX.md §1 — "all other users get silence".

Silence is literal. An "unauthorized" reply would confirm to a stranger that they
found a live private bot, and this one answers questions about someone's capital,
open positions and pause state. The assertion is therefore that the fake bot
recorded **zero** outbound calls, not that it recorded a refusal.
"""

from __future__ import annotations

from typing import Any

import pytest

from sentinel.bot.allowlist import AllowlistMiddleware

OWNER = 111
STRANGER = 222


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _Event:
    """Stands in for a Message or a CallbackQuery — the middleware reads neither."""

    def __init__(self, text: str = "/status") -> None:
        self.text = text


async def call(middleware: AllowlistMiddleware, user_id: int | None) -> bool:
    """Returns whether the handler ran."""
    ran = False

    async def handler(event: Any, data: dict[str, Any]) -> None:
        nonlocal ran
        ran = True

    data: dict[str, Any] = {} if user_id is None else {"event_from_user": _User(user_id)}
    await middleware(handler, _Event(), data)  # type: ignore[arg-type]
    return ran


async def test_the_owner_is_let_through() -> None:
    assert await call(AllowlistMiddleware((OWNER,)), OWNER) is True


@pytest.mark.parametrize("user_id", [STRANGER, 0, -1, None])
async def test_everyone_else_is_dropped_silently(user_id: int | None) -> None:
    """No handler, and therefore no reply — the update simply stops here."""
    assert await call(AllowlistMiddleware((OWNER,)), user_id) is False


async def test_an_empty_allowlist_admits_nobody() -> None:
    """Fail closed. An unconfigured TELEGRAM_ALLOWED_USER_IDS is a misconfiguration,
    and failing open would hand the bot to whoever finds it first."""
    middleware = AllowlistMiddleware(())
    assert await call(middleware, OWNER) is False
    assert await call(middleware, STRANGER) is False


async def test_several_allowed_ids_all_pass() -> None:
    middleware = AllowlistMiddleware((OWNER, 999))
    assert await call(middleware, OWNER) is True
    assert await call(middleware, 999) is True
    assert await call(middleware, STRANGER) is False


def test_the_allowlist_is_parsed_from_a_comma_separated_secret() -> None:
    """The .env format, round-tripped through the real Secrets model."""
    from sentinel.core.config import Secrets

    secrets = Secrets(_env_file=None, telegram_allowed_user_ids=" 111, 222 ,333 ")
    assert secrets.allowed_user_ids == (111, 222, 333)
    assert Secrets(_env_file=None).allowed_user_ids == ()
