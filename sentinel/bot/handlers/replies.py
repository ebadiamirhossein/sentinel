"""§2's manage row: 🔚 Closed manually and ✏️ Note.

"manual close asks for exit price to record honest realized R" — so the owner has
to *tell us a number*, and a bot has to remember it was asking.

**No FSM state.** aiogram's usual answer is a finite-state machine keyed on the
user, held in memory and lost on restart — which for a trading system means a
process restart mid-question silently swallows the next thing the owner types, or
worse, reads it as an answer to a question nobody remembers asking. Instead the
prompt is sent with ``ForceReply`` and the *reply* carries the context: Telegram
tells us which message was replied to, and that message id is claimed in
``telegram_messages`` with an ``event_key`` naming the signal and the action. The
question is therefore as durable as the database, and a restart between question
and answer costs nothing.

An exit price is validated the same way ``/capital`` is — a NaN reaching realized
R would poison every statistic downstream (M6 §7 found exactly that bug).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, ForceReply, Message

from sentinel.bot.auth import Actor
from sentinel.bot.context import BotContext
from sentinel.bot.formatting import escape
from sentinel.bot.keyboards import ManageAction, ManageCallback
from sentinel.bot.models import MessageKind, SignalStatus
from sentinel.core.logging import get_logger
from sentinel.risk.accounting import Exit, Fill, realized_r
from sentinel.risk.models import TradePlan
from sentinel.risk.rounding import money, ratio
from sentinel.tracker.models import EXIT_MANUAL, EventKind

log = get_logger(__name__)

replies_router = Router(name="replies")

CLOSE_PROMPT = (
    "🔚 <b>Closed manually</b> — reply to this message with the price you were "
    "actually filled at.\nThat is what makes the realized R honest; the target "
    "price would only be a guess."
)
NOTE_PROMPT = "✏️ Reply to this message with a note to store against this signal."

#: ``event_key`` of the prompt message, so the reply can find its signal again.
PROMPT_KEY = "manage_prompt"


def _prompt_key(action: ManageAction) -> str:
    return f"{PROMPT_KEY}:{action.value}"


@replies_router.callback_query(ManageCallback.filter())
async def manage(
    query: CallbackQuery, callback_data: ManageCallback, ctx: BotContext, actor: Actor
) -> None:
    """Ask the question, and record that we asked it."""
    assert query.message is not None
    chat_id = query.message.chat.id
    action = callback_data.action

    async with ctx.database.session() as session:
        row = await ctx.repositories.signals(session).get(callback_data.signal_id)
    if row is None:
        await query.answer("That signal is no longer in the database.", show_alert=True)
        return
    if row.user_id != actor.user_id:
        # As with the decision buttons (M8.1): a forwarded card carries its manage
        # row, and a manual close writes a realized R into somebody's statistics.
        log.warning(
            "bot.manage_rejected",
            signal_id=str(callback_data.signal_id),
            user_id=actor.user_id,
            detail="the signal belongs to another user",
        )
        await query.answer("That signal is not yours.", show_alert=True)
        return
    if action is ManageAction.CLOSE and row.filled_qty <= 0:
        await query.answer("Nothing has filled on this signal yet.", show_alert=True)
        return

    await query.answer()
    text = CLOSE_PROMPT if action is ManageAction.CLOSE else NOTE_PROMPT
    try:
        prompt = await query.message.reply(text, reply_markup=ForceReply(selective=True))
    except (TelegramBadRequest, AttributeError):  # pragma: no cover — cosmetic only
        log.info("bot.manage_prompt_failed", signal_id=str(callback_data.signal_id))
        return

    async with ctx.database.session() as session:
        messages = ctx.repositories.messages(session)
        await messages.claim(
            callback_data.signal_id,
            MessageKind.UPDATE,
            chat_id,
            at=ctx.clock.now(),
            event_key=_prompt_key(action),
        )
        await messages.confirm(
            callback_data.signal_id,
            MessageKind.UPDATE,
            chat_id,
            message_id=int(prompt.message_id),
            at=ctx.clock.now(),
            event_key=_prompt_key(action),
        )
        await session.commit()


@replies_router.message(F.reply_to_message)
async def answered(message: Message, ctx: BotContext) -> None:
    """A reply to one of our prompts. Anything else is ignored in silence."""
    assert message.reply_to_message is not None
    replied_to = message.reply_to_message.message_id
    chat_id = message.chat.id

    found = await _find_prompt(ctx, chat_id, replied_to)
    if found is None:
        return
    signal_id, action = found

    if action is ManageAction.NOTE:
        await _record_note(message, ctx, signal_id)
        return
    await _record_manual_close(message, ctx, signal_id)


async def _find_prompt(
    ctx: BotContext, chat_id: int, message_id: int
) -> tuple[UUID, ManageAction] | None:
    """Which signal and question this reply belongs to, from the database.

    The lookup that replaces an FSM: the prompt's own message id is the key, so
    the conversation survives a restart and cannot be confused with an unrelated
    message the owner happens to send next.
    """
    async with ctx.database.session() as session:
        rows = await ctx.repositories.messages(session).claimed_message(chat_id, message_id)
    if rows is None:
        return None
    signal_id, event_key = rows
    for action in ManageAction:
        if event_key == _prompt_key(action):
            return signal_id, action
    return None


async def _record_note(message: Message, ctx: BotContext, signal_id: UUID) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.reply("❌ Nothing to store.")
        return

    async with ctx.database.session() as session:
        await ctx.repositories.events(session).record(
            signal_id,
            event_key=f"note:{int(ctx.clock.now().timestamp())}",
            kind=EventKind.NOTE.value,
            at=ctx.clock.now(),
            detail=text[:512],
        )
        await session.commit()
    log.info("bot.note_recorded", signal_id=str(signal_id))
    await message.reply(f"✏️ Noted: {escape(text[:200])}")


async def _record_manual_close(message: Message, ctx: BotContext, signal_id: UUID) -> None:
    """Record the owner's own exit, and the realized R it produces.

    The arithmetic is ``risk/accounting.py``'s, on the fills the tracker actually
    saw — the same basis as every other R figure, so a manual close and a stop-out
    are directly comparable in ``/stats``.
    """
    price = _parse_price(message.text)
    if price is None:
        await message.reply("❌ That is not a price. Reply with a number, e.g. <code>84.10</code>.")
        return

    async with ctx.database.session() as session:
        signals = ctx.repositories.signals(session)
        row = await signals.get(signal_id)
        if row is None:  # pragma: no cover — the prompt proved it existed
            await message.reply("❌ That signal is no longer in the database.")
            return

        plan = TradePlan.model_validate(row.plan)
        fills = await ctx.repositories.fills(session).for_signal(signal_id)
        exits = await ctx.repositories.exits(session).for_signal(signal_id)
        open_qty = row.filled_qty - sum((exit_.qty for exit_ in exits), Decimal(0))
        if open_qty <= 0:
            await message.reply("That position is already fully closed.")
            return

        closed = (
            *(Exit(price=exit_.price, qty=exit_.qty) for exit_ in exits if exit_.qty > 0),
            Exit(price=price, qty=open_qty),
        )
        r = realized_r(
            direction=plan.direction,
            fills=tuple(Fill(price=fill.price, qty=fill.qty) for fill in fills),
            exits=closed,
            planned_risk_usdt=plan.planned_risk_eur * plan.eurusd_rate,
        )
        now = ctx.clock.now()

        await ctx.repositories.exits(session).record(
            signal_id,
            kind=EXIT_MANUAL,
            price=price,
            qty=open_qty,
            exited_at=now,
            detected_at=now,
        )
        await ctx.repositories.events(session).record(
            signal_id,
            event_key="closed_manually",
            kind=EventKind.CLOSED_MANUALLY.value,
            at=now,
            from_status=row.status,
            to_status=SignalStatus.CLOSED.value,
            price=price,
            realized_r=ratio(r),
            realized_eur=money(r * plan.planned_risk_eur),
            payload={"qty": str(open_qty)},
            detail="Closed manually",
        )
        await signals.advance(
            signal_id,
            status=SignalStatus.CLOSED.value,
            outcome=EXIT_MANUAL,
            realized_r=ratio(r),
            realized_eur=money(r * plan.planned_risk_eur),
            closed_at=now,
        )
        await session.commit()

    log.info(
        "bot.manual_close_recorded",
        signal_id=str(signal_id),
        price=str(price),
        realized_r=str(ratio(r)),
    )
    await message.reply(
        f"🔚 Recorded a manual close at {price} → {ratio(r)}R "
        f"(€{money(r * plan.planned_risk_eur)})."
    )


def _parse_price(raw: str | None) -> Decimal | None:
    """A price, or nothing. NaN and infinity are rejected by name.

    ``Decimal("nan")`` parses cleanly and would poison every figure derived from
    it — M6 §7 found exactly that in ``/capital``, and this input feeds the same
    arithmetic.
    """
    if raw is None:
        return None
    try:
        value = Decimal(raw.strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


__all__ = ["CLOSE_PROMPT", "NOTE_PROMPT", "PROMPT_KEY", "replies_router"]
