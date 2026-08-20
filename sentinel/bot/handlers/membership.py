"""Joining, understanding, and leaving — specs/TELEGRAM_UX.md §7 (M8.1).

``/start``, ``/help``, ``/leave``, and the first-run acknowledgement button. These
are the only handlers a caller without full standing can reach, which is why they
live apart from both the member commands and the owner's.

Three properties worth stating, because each is a decision rather than an
implementation detail:

* **One request per id, guaranteed by a primary key.** ``/start`` inserts with
  ``ON CONFLICT DO NOTHING``; only an insert that actually created a row notifies
  the owner. A stranger tapping ``/start`` twenty times therefore produces one
  message, and a restart cannot lose or duplicate that count because there is no
  count to lose.
* **Nothing is delivered before the note is acknowledged**, and the acknowledgement
  records *which wording* was accepted. A timestamp alone would assert consent to
  words nobody can identify.
* **Leaving needs nobody's permission.** ``/leave`` is a member's own decision, one
  confirmation deep. Their history stays — deleting measured history is never
  automatic — but they receive nothing further and show as LEFT to the owner.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sentinel.bot.auth import Actor
from sentinel.bot.cards import (
    acknowledgement_card,
    help_card,
    leave_confirm_card,
    left_card,
    ready_card,
    registration_request_card,
    standing_card,
)
from sentinel.bot.context import BotContext
from sentinel.bot.keyboards import (
    AckCallback,
    LeaveCallback,
    acknowledge_keyboard,
    approval_keyboard,
    leave_keyboard,
)
from sentinel.bot.menu import clear_for
from sentinel.bot.models import ACK_VERSION, UserStatus
from sentinel.bot.outbound import SupportsBot
from sentinel.core.logging import get_logger

log = get_logger(__name__)

membership_router = Router(name="membership")

#: What the owner is told when they try to remove themselves. The owner is the only
#: account that can approve anybody, so an owner who left would leave a system with
#: users and no operator — and no way back in from inside Telegram.
OWNER_CANNOT_LEAVE = (
    "You are the owner — /leave would leave this system with no one able to approve, "
    "suspend or operate anything. Suspend the members you want to stop, or stop the "
    "service on the server."
)


@membership_router.message(Command("help"))
async def help_command(message: Message, ctx: BotContext) -> None:
    """§3 ``/help`` — plain language, for somebody who does not read the specs.

    Reachable while still unacknowledged on purpose: the note asks a person to
    accept that this is experimental and unmeasured, and they should be able to read
    what the numbers mean before deciding.
    """
    for page in help_card():
        await message.answer(page)


@membership_router.message(Command("start"))
async def start(message: Message, ctx: BotContext, actor: Actor, bot: SupportsBot) -> None:
    """§7 — a stranger's one crack in the door, and a set-up caller's summary."""
    if actor.account is not None:
        await message.answer(ready_card(capital_set=actor.account.capital_set))
        return

    now = ctx.clock.now()
    async with ctx.database.session() as session:
        users = ctx.repositories.users(session)
        created = await users.request(
            actor.user_id,
            username=actor.username,
            display_name=actor.display_name,
            at=now,
        )
        if created is None:  # pragma: no cover — the gate loaded no row a moment ago
            return
        await users.touch_notice(actor.user_id, at=now)
        owner = await users.owner()
        await session.commit()

    log.info("bot.access_requested", user_id=actor.user_id, username=actor.username)
    await message.answer(standing_card(created))

    if owner is None:
        log.warning(
            "bot.no_owner",
            user_id=actor.user_id,
            detail="an access request was stored but there is no OWNER row to notify",
        )
        return
    await bot.send_message(
        chat_id=owner.telegram_user_id,
        text=registration_request_card(created, ctx.tz),
        parse_mode=ctx.settings.config.telegram.parse_mode,
        reply_markup=approval_keyboard(actor.user_id),
        reply_to_message_id=None,
    )


@membership_router.callback_query(AckCallback.filter())
async def acknowledge(
    query: CallbackQuery, callback_data: AckCallback, ctx: BotContext, actor: Actor
) -> None:
    """§7's first-run acknowledgement. Signals wait on this and nothing else does."""
    account = actor.known()
    if callback_data.version != ACK_VERSION:
        # A button from a superseded note. Recording it would log consent to wording
        # this person never saw, so the current note is sent instead.
        await query.answer("This note has been updated — please read the new one.")
        if query.message is not None:
            await query.message.answer(acknowledgement_card(), reply_markup=acknowledge_keyboard())
        return

    now = ctx.clock.now()
    async with ctx.database.session() as session:
        updated = await ctx.repositories.users(session).acknowledge(
            actor.user_id, version=ACK_VERSION, at=now
        )
        await session.commit()

    log.info("bot.acknowledged", user_id=actor.user_id, version=ACK_VERSION)
    await query.answer("Thank you.")
    capital_set = account.capital_set if updated is None else updated.capital_set
    if query.message is not None:
        await query.message.answer(ready_card(capital_set=capital_set))


@membership_router.message(Command("leave"))
async def leave(message: Message, ctx: BotContext, actor: Actor) -> None:
    """§7 ``/leave`` — a member's own exit, one confirmation deep."""
    if actor.is_owner:
        await message.answer(OWNER_CANNOT_LEAVE)
        return
    await message.answer(leave_confirm_card(), reply_markup=leave_keyboard())


@membership_router.callback_query(LeaveCallback.filter())
async def leave_confirmation(
    query: CallbackQuery,
    callback_data: LeaveCallback,
    ctx: BotContext,
    actor: Actor,
    bot: SupportsBot,
) -> None:
    if not callback_data.confirm:
        await query.answer("Still here.")
        if query.message is not None:
            await query.message.answer("↩️ Nothing changed — you are still set up.")
        return
    if actor.is_owner:  # pragma: no cover — /leave refuses the owner before this
        await query.answer("The owner cannot leave.", show_alert=True)
        return

    now = ctx.clock.now()
    async with ctx.database.session() as session:
        await ctx.repositories.users(session).set_status(
            actor.user_id, UserStatus.LEFT, at=now, by_user_id=actor.user_id
        )
        await session.commit()

    # The menu goes with the access: leaving ``/positions`` and ``/stats`` on the
    # "/" list of somebody the gate now answers with silence is a broken promise.
    await clear_for(bot, actor.user_id)
    log.info("bot.left", user_id=actor.user_id)
    await query.answer("Removed.")
    if query.message is not None:
        await query.message.answer(left_card())


__all__ = ["OWNER_CANNOT_LEAVE", "membership_router"]
