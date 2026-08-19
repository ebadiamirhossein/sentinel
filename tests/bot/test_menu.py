"""The ``/`` command menu — ``setMyCommands``, scoped per caller (M8.1).

Nothing registered a menu through M8, so typing ``/`` listed nothing and every
command had to be remembered. The menu is also an access-control surface: it must
never advertise an owner command to somebody the gate would answer with silence,
because that would tell them the command exists and that somebody has it.
"""

from __future__ import annotations

from typing import Any

import pytest

from sentinel.bot.menu import (
    MEMBER_COMMANDS,
    OWNER_COMMANDS,
    PUBLIC_COMMANDS,
    commands_for,
    publish_menu,
)
from tests.bot_double import FakeBot

OWNER = 111
MEMBER = 222


def names(commands: Any) -> set[str]:
    return {command.command for command in commands}


def test_a_stranger_sees_only_start_and_help() -> None:
    assert names(PUBLIC_COMMANDS) == {"start", "help"}


def test_the_member_menu_carries_no_owner_command() -> None:
    assert names(MEMBER_COMMANDS) & names(OWNER_COMMANDS) == set()
    assert "approve" not in names(commands_for(owner=False))
    assert "users" not in names(commands_for(owner=False))


def test_the_member_menu_is_exactly_what_a_member_may_run() -> None:
    """The owner ruling on scope, restated as the thing a person actually sees."""
    assert names(MEMBER_COMMANDS) == {
        "help",
        "capital",
        "risk",
        "positions",
        "stats",
        "request",
        "leave",
    }


def test_the_owner_gets_both_halves_except_leave_and_request() -> None:
    owner = names(commands_for(owner=True))
    assert names(OWNER_COMMANDS) <= owner
    assert names(MEMBER_COMMANDS) - {"leave", "request"} <= owner
    assert "leave" not in owner, (
        "the handler refuses an owner who tries to leave, and a command that always "
        "says no should not be offered"
    )
    assert "request" not in owner, (
        "the owner has /watchlist add; a request path would have them approving "
        "their own card (M8.3)"
    )


def test_every_command_has_a_description_short_enough_for_telegram() -> None:
    """Telegram caps a command description at 256 characters and silently rejects
    the whole call if one is over, which would leave everybody with no menu."""
    for command in (*PUBLIC_COMMANDS, *MEMBER_COMMANDS, *OWNER_COMMANDS):
        assert command.description, f"/{command.command} has no description"
        assert len(command.description) <= 256


async def test_startup_publishes_the_default_and_one_scope_per_approved_user() -> None:
    bot = FakeBot()
    await publish_menu(bot, owner_id=OWNER, member_ids=[OWNER, MEMBER])

    scopes = bot.scopes()
    assert set(scopes) == {"BotCommandScopeAllPrivateChats", str(OWNER), str(MEMBER)}
    assert set(scopes["BotCommandScopeAllPrivateChats"]) == {"start", "help"}
    assert "approve" in scopes[str(OWNER)]
    assert "approve" not in scopes[str(MEMBER)], "a member is not told the command exists"


async def test_a_deployment_with_no_owner_still_publishes_the_public_menu() -> None:
    bot = FakeBot()
    await publish_menu(bot, owner_id=None, member_ids=[])
    assert set(bot.scopes()) == {"BotCommandScopeAllPrivateChats"}


async def test_one_failing_chat_does_not_cost_everyone_else_their_menu() -> None:
    """A member who blocked the bot is a normal Tuesday, not an outage."""
    bot = FakeBot(fail={"set_my_commands": RuntimeError("chat not found")})
    await publish_menu(bot, owner_id=OWNER, member_ids=[OWNER, MEMBER])

    # Every scope was still attempted: the failure is swallowed per scope, so the
    # loop does not stop at the first bad chat id.
    assert len(bot.of("set_my_commands")) == 3


async def test_a_failed_menu_never_reaches_the_caller() -> None:
    """``BotRunner.start`` calls this before polling begins. If it raised, a broken
    menu would stop the bot from answering at all — the menu is a convenience and
    the signals are the product."""
    bot = FakeBot(fail={"set_my_commands": RuntimeError("telegram is down")})
    await publish_menu(bot, owner_id=OWNER, member_ids=[])


@pytest.mark.parametrize("owner", [True, False])
def test_help_is_always_in_the_menu(owner: bool) -> None:
    """It is the one command that explains every number on a card."""
    assert "help" in names(commands_for(owner=owner))
