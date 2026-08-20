"""The ``/`` command menu — ``setMyCommands``, scoped per caller (M8.1).

Nothing registered a command menu through M8, so typing ``/`` in the chat listed
nothing and every command had to be remembered. That is a usability gap for the
owner and an outright barrier for anyone else.

**Three scopes, because the menu is also an access-control surface.** Telegram lets
commands be registered per chat, and a menu that advertised ``/approve`` to everyone
would tell every member that owner commands exist and who might have them — the
exact opposite of :class:`~sentinel.bot.auth.OwnerOnly`'s silence. So:

* every private chat sees ``/start`` and ``/help`` — that is all a stranger gets;
* an approved member's own chat gets the member set;
* the owner's own chat gets the member set plus the owner set.

The per-chat scopes are (re)published at startup for everyone currently approved,
and adjusted the moment standing changes: set on approval, cleared on rejection,
suspension or ``/leave``, at which point the person falls back to the default
two-command menu and the bot stops advertising what it will no longer do for them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from sentinel.core.logging import get_logger

log = get_logger(__name__)

#: What a stranger — or anyone whose access has ended — sees.
PUBLIC_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="start", description="Request access / check where you stand"),
    BotCommand(command="help", description="What the numbers mean, in plain language"),
)

#: An approved member's menu. Owner-only commands are deliberately absent, and the
#: order is the order somebody actually needs them in.
MEMBER_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="help", description="What the numbers mean, in plain language"),
    BotCommand(command="capital", description="Set your capital in EUR — /capital 10000"),
    BotCommand(command="risk", description="Set your risk per trade % — /risk 0.75"),
    BotCommand(command="positions", description="Your open signals, marked to market"),
    BotCommand(command="stats", description="Your win rate, avg R, profit factor"),
    BotCommand(command="pulse", description="What the pipeline did — 24h, or a symbol"),
    BotCommand(command="snapshot", description="The measured numbers — /snapshot SOLUSDT"),
    BotCommand(command="journal", description="Your signals as a spreadsheet"),
    BotCommand(command="request", description="Ask for a symbol — /request SOLUSDT"),
    BotCommand(command="leave", description="Stop receiving signals and remove yourself"),
)

#: The operator's half. Appended to the member set for the owner's chat only.
OWNER_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="status", description="Pipeline health, rails, spend"),
    BotCommand(command="settings", description="Every runtime value that shapes a signal"),
    BotCommand(command="watchlist", description="View or edit the scanned symbols"),
    BotCommand(command="pause", description="Stop gating new signals, system-wide"),
    BotCommand(command="resume", description="Resume after a pause"),
    BotCommand(command="users", description="Who has access, and who is waiting"),
    BotCommand(command="approve", description="Approve a request — /approve <id>"),
    BotCommand(command="reject", description="Decline a request — /reject <id>"),
    BotCommand(command="suspend", description="Suspend a member — /suspend <id>"),
)


class SupportsCommands(Protocol):
    """Just the two Bot methods this module uses.

    Narrow on purpose: ``SupportsSending`` is the protocol for *messages*, and the
    publisher, the notifier and the alerter all depend on it. A command menu is not
    a message, so widening that protocol would make three components declare a
    capability none of them needs.

    The signatures name the exact keywords used, exactly as ``SupportsSending``
    does, so ``mypy --strict`` checks that a real ``aiogram.Bot`` satisfies this — a
    ``**kwargs: Any`` protocol would accept anything and prove nothing.
    """

    async def set_my_commands(
        self,
        *,
        commands: list[BotCommand],
        scope: BotCommandScopeAllPrivateChats | BotCommandScopeChat,
    ) -> Any: ...

    async def delete_my_commands(self, *, scope: BotCommandScopeChat) -> Any: ...


def commands_for(*, owner: bool) -> tuple[BotCommand, ...]:
    """One caller's menu.

    Two are dropped for the owner, for the same reason. ``/leave``: the handler
    refuses it — an owner who left would leave the system with users and nobody able
    to operate it. ``/request`` (M8.3): the owner has ``/watchlist add``, which does
    the thing directly, and a request path would have them approving their own card.
    Both handlers still answer if typed; offering a command that always redirects is
    worse than not offering it.
    """
    if not owner:
        return MEMBER_COMMANDS
    dropped = {"leave", "request"}
    keeps = tuple(command for command in MEMBER_COMMANDS if command.command not in dropped)
    return (*keeps, *OWNER_COMMANDS)


async def publish_default(bot: SupportsCommands) -> None:
    """The two-command menu every private chat falls back to."""
    await bot.set_my_commands(
        commands=list(PUBLIC_COMMANDS), scope=BotCommandScopeAllPrivateChats()
    )


async def publish_for(bot: SupportsCommands, user_id: int, *, owner: bool) -> None:
    """Give one chat the menu its standing earns."""
    await bot.set_my_commands(
        commands=list(commands_for(owner=owner)), scope=BotCommandScopeChat(chat_id=user_id)
    )


async def clear_for(bot: SupportsCommands, user_id: int) -> None:
    """Drop a chat's menu, so it falls back to :data:`PUBLIC_COMMANDS`.

    Called when standing ends. Leaving the member menu in place would keep offering
    ``/positions`` and ``/stats`` to somebody the gate now answers with silence.
    """
    await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))


async def publish_menu(
    bot: SupportsCommands, *, owner_id: int | None, member_ids: Iterable[int]
) -> None:
    """Publish every scope at startup. Never raises.

    A failed ``setMyCommands`` must not stop the bot from polling: the menu is a
    convenience and the signals are the product. Each scope is attempted
    independently so one bad chat id — a member who blocked the bot, say — does not
    cost everyone else their menu.
    """
    members: Sequence[int] = [uid for uid in member_ids if uid != owner_id]
    await _attempt(publish_default(bot), scope="default")
    if owner_id is not None:
        await _attempt(publish_for(bot, owner_id, owner=True), scope=f"chat:{owner_id}")
    for user_id in members:
        await _attempt(publish_for(bot, user_id, owner=False), scope=f"chat:{user_id}")


async def _attempt(action: Any, *, scope: str) -> None:
    try:
        await action
    except Exception:
        log.warning("bot.menu_failed", scope=scope, exc_info=True)


__all__ = [
    "MEMBER_COMMANDS",
    "OWNER_COMMANDS",
    "PUBLIC_COMMANDS",
    "SupportsCommands",
    "clear_for",
    "commands_for",
    "publish_default",
    "publish_for",
    "publish_menu",
]
