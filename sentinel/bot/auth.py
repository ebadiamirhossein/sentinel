"""Who may talk to this bot, and what happens to everyone else (M8.1).

This replaces ``allowlist.py``. Through M8 authorization was one env var and one
rule — "all other users get silence" (specs/TELEGRAM_UX.md §1) — which is exactly
right for a system with one user and impossible for a system somebody has to be
able to *join*. The front door now opens exactly one crack: **``/start`` from an
unknown id is answered; nothing else is.**

The decision is a truth table over (standing, update kind), stated in full in
``TABLE`` and checked for completeness by a meta-test. This is the gate on other
people's money and on the privacy of their books, so "which combinations did we
think of" must be a list nobody can silently leave a hole in — the same discipline
``tracker/machine.py`` applies to the state machine.

Four things it deliberately does **not** do:

* It never answers a non-``/start`` update from a non-approved id. A reply would
  confirm to a stranger that they found a live private bot, and this one talks
  about somebody's capital and open positions.
* It never lets a rejected id hold a conversation: a standing notice is throttled
  to one per ``NOTICE_THROTTLE`` per id, recorded in ``users.notice_at``.
* It never decides *role*. Owner-only commands live behind :class:`OwnerOnly` on
  their own router, so a member's ``/approve`` matches no handler and produces
  silence rather than a refusal that reveals the command exists.
* It never re-approves anybody. Standing changes only through the owner's
  commands, or the user's own ``/leave``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, cast

from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from sentinel.bot.cards import acknowledgement_card, standing_card
from sentinel.bot.context import BotContext
from sentinel.bot.keyboards import ACK_PREFIX, acknowledge_keyboard
from sentinel.bot.models import UserAccount, UserStatus
from sentinel.core.logging import get_logger

log = get_logger(__name__)

#: How often one id may be told where it stands. A rejected stranger who keeps
#: pressing /start gets one answer an hour, not a correspondent.
NOTICE_THROTTLE = timedelta(hours=1)

#: What a caller may be asked to acknowledge before anything else works.
ACK_NUDGE = "Please read and accept the note above before signals start."


class Standing(StrEnum):
    """The caller's position, collapsed to what authorization actually turns on."""

    #: No ``users`` row at all — a stranger.
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"
    LEFT = "LEFT"
    #: Approved, but has not accepted the current first-run acknowledgement.
    UNACKNOWLEDGED = "UNACKNOWLEDGED"
    ACTIVE = "ACTIVE"


class UpdateKind(StrEnum):
    """The only distinctions the gate makes between updates."""

    START = "START"
    HELP = "HELP"
    #: The acknowledgement button, which must work before anything else does.
    ACK = "ACK"
    OTHER = "OTHER"


class Verdict(StrEnum):
    PASS = "PASS"
    #: Silence. The update stops here and nothing is sent.
    DROP = "DROP"
    #: Say where they stand, at most once per :data:`NOTICE_THROTTLE`.
    NOTICE = "NOTICE"
    #: Re-send the first-run acknowledgement; signals wait on it.
    ACK_REQUIRED = "ACK_REQUIRED"


#: Every (standing, update kind) pair, stated rather than derived. A missing cell
#: is a hole in the gate, and ``test_auth.py`` asserts the keys are the full
#: cartesian product — because nothing else tells you what you forgot.
TABLE: dict[tuple[Standing, UpdateKind], Verdict] = {
    # A stranger may ask, once, and learn nothing else about this bot.
    (Standing.UNKNOWN, UpdateKind.START): Verdict.PASS,
    (Standing.UNKNOWN, UpdateKind.HELP): Verdict.DROP,
    (Standing.UNKNOWN, UpdateKind.ACK): Verdict.DROP,
    (Standing.UNKNOWN, UpdateKind.OTHER): Verdict.DROP,
    # Waiting on the owner. Told so, throttled — silence here reads as a broken
    # bot and produces another /start a minute later.
    (Standing.PENDING, UpdateKind.START): Verdict.NOTICE,
    (Standing.PENDING, UpdateKind.HELP): Verdict.DROP,
    (Standing.PENDING, UpdateKind.ACK): Verdict.DROP,
    (Standing.PENDING, UpdateKind.OTHER): Verdict.DROP,
    # Refused, suspended, or gone by their own choice. All three are the same
    # silence and completely different sentences, so the notice differs and the
    # authorization does not.
    (Standing.REJECTED, UpdateKind.START): Verdict.NOTICE,
    (Standing.REJECTED, UpdateKind.HELP): Verdict.DROP,
    (Standing.REJECTED, UpdateKind.ACK): Verdict.DROP,
    (Standing.REJECTED, UpdateKind.OTHER): Verdict.DROP,
    (Standing.SUSPENDED, UpdateKind.START): Verdict.NOTICE,
    (Standing.SUSPENDED, UpdateKind.HELP): Verdict.DROP,
    (Standing.SUSPENDED, UpdateKind.ACK): Verdict.DROP,
    (Standing.SUSPENDED, UpdateKind.OTHER): Verdict.DROP,
    (Standing.LEFT, UpdateKind.START): Verdict.NOTICE,
    (Standing.LEFT, UpdateKind.HELP): Verdict.DROP,
    (Standing.LEFT, UpdateKind.ACK): Verdict.DROP,
    (Standing.LEFT, UpdateKind.OTHER): Verdict.DROP,
    # Approved and yet to accept the note. /help is allowed on purpose: the note
    # says the system is experimental and unmeasured, and somebody deciding whether
    # to accept that should be able to read what it means first.
    (Standing.UNACKNOWLEDGED, UpdateKind.START): Verdict.ACK_REQUIRED,
    (Standing.UNACKNOWLEDGED, UpdateKind.HELP): Verdict.PASS,
    (Standing.UNACKNOWLEDGED, UpdateKind.ACK): Verdict.PASS,
    (Standing.UNACKNOWLEDGED, UpdateKind.OTHER): Verdict.ACK_REQUIRED,
    (Standing.ACTIVE, UpdateKind.START): Verdict.PASS,
    (Standing.ACTIVE, UpdateKind.HELP): Verdict.PASS,
    (Standing.ACTIVE, UpdateKind.ACK): Verdict.PASS,
    (Standing.ACTIVE, UpdateKind.OTHER): Verdict.PASS,
}


@dataclass(frozen=True)
class Actor:
    """The caller, as every handler receives them.

    ``account`` is ``None`` only for the one update the gate lets through without a
    row: a stranger's ``/start``. Every other handler may call :meth:`known`.
    """

    user_id: int
    username: str | None = None
    display_name: str | None = None
    account: UserAccount | None = None

    @property
    def is_owner(self) -> bool:
        return self.account is not None and self.account.is_owner

    def known(self) -> UserAccount:
        assert self.account is not None, "the gate admits no unknown caller here"
        return self.account


def standing_of(account: UserAccount | None) -> Standing:
    """Collapse a row to the seven positions authorization turns on."""
    if account is None:
        return Standing.UNKNOWN
    if account.status is UserStatus.APPROVED:
        return Standing.ACTIVE if account.acknowledged else Standing.UNACKNOWLEDGED
    return Standing(account.status.value)


def is_callback(event: TelegramObject) -> bool:
    """Whether this update is a button press rather than a message.

    Duck-typed rather than ``isinstance``, deliberately and for one reason: the whole
    suite drives this layer with the doubles in ``tests/bot_double.py``, and a real
    ``aiogram.Message`` cannot be constructed without a bound ``Bot`` to answer
    through. ``data`` is present on ``CallbackQuery`` and absent on ``Message``, so
    the discriminator is exactly as sharp as the type check would be.
    """
    return hasattr(event, "data")


def classify(event: TelegramObject) -> UpdateKind:
    """Which of the four kinds this update is.

    Commands are matched on the text rather than through aiogram's filters because
    this runs *before* routing — the point of the gate is that an unauthorized
    update never reaches a router at all.
    """
    if is_callback(event):
        payload = getattr(event, "data", None) or ""
        return UpdateKind.ACK if payload.startswith(ACK_PREFIX) else UpdateKind.OTHER
    text = getattr(event, "text", None)
    if isinstance(text, str):
        word = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
        if word == "/start":
            return UpdateKind.START
        if word == "/help":
            return UpdateKind.HELP
    return UpdateKind.OTHER


class AuthMiddleware(BaseMiddleware):
    """Apply :data:`TABLE` to every message and callback query.

    Registered on both observers, as the allowlist was: a card forwarded to a
    stranger carries its buttons with it, and a button is a perfectly good way to
    try to write to somebody else's database.
    """

    def __init__(self, ctx: BotContext) -> None:
        self._ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            log.warning("bot.rejected_update", user_id=None, update_type=type(event).__name__)
            return None

        async with self._ctx.database.session() as session:
            account = await self._ctx.repositories.users(session).get(user.id)

        kind = classify(event)
        verdict = TABLE[(standing_of(account), kind)]
        if verdict is Verdict.PASS:
            data["actor"] = Actor(
                user_id=user.id,
                username=user.username,
                display_name=user.full_name,
                account=account,
            )
            return await handler(event, data)

        log.info(
            "bot.update_gated",
            user_id=user.id,
            standing=standing_of(account).value,
            kind=kind.value,
            verdict=verdict.value,
        )
        if verdict is Verdict.NOTICE:
            await self._notify(event, account)
        elif verdict is Verdict.ACK_REQUIRED:
            await self._ask_acknowledgement(event, account)
        return None

    async def _notify(self, event: TelegramObject, account: UserAccount | None) -> None:
        """Say where they stand — once per :data:`NOTICE_THROTTLE`, never more."""
        if account is None or is_callback(event):  # pragma: no cover — NOTICE is /start
            return
        now = self._ctx.clock.now()
        if account.notice_at is not None and now - account.notice_at < NOTICE_THROTTLE:
            return
        async with self._ctx.database.session() as session:
            await self._ctx.repositories.users(session).touch_notice(
                account.telegram_user_id, at=now
            )
            await session.commit()
        await _reply(event, standing_card(account))

    async def _ask_acknowledgement(
        self, event: TelegramObject, account: UserAccount | None
    ) -> None:
        """Approved but unacknowledged: re-send the note instead of going quiet."""
        if account is None:  # pragma: no cover — only ACTIVE-adjacent states get here
            return
        if is_callback(event):
            await _toast(event, ACK_NUDGE)
            return
        await _reply(event, acknowledgement_card(), reply_markup=acknowledge_keyboard())


async def _reply(event: TelegramObject, text: str, reply_markup: Any = None) -> None:
    message = cast(Message, event)
    await message.answer(text, reply_markup=reply_markup)


async def _toast(event: TelegramObject, text: str) -> None:
    query = cast(CallbackQuery, event)
    await query.answer(text, show_alert=True)


class OwnerOnly(BaseFilter):
    """Owner-only handlers, and the reason there is no refusal message.

    A filter that does not match means no handler runs, which *is* silence. A member
    typing ``/approve`` therefore learns nothing — not that the command exists, not
    that they lack the role, not that anyone else does.
    """

    async def __call__(self, event: TelegramObject, actor: Actor) -> bool:
        return actor.is_owner


__all__ = [
    "ACK_NUDGE",
    "NOTICE_THROTTLE",
    "TABLE",
    "Actor",
    "AuthMiddleware",
    "OwnerOnly",
    "Standing",
    "UpdateKind",
    "Verdict",
    "classify",
    "is_callback",
    "standing_of",
]
