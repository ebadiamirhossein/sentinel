"""Inline-button callbacks — specs/TELEGRAM_UX.md §2.

Taken / Watching / Skip is the single most consequential input the owner gives
this system: it decides whether an outcome lands in the **real** statistics or
the hypothetical ones, and at M7 whether the signal consumes the open-risk
budget. So it is persisted before the message is edited, and a failed edit never
loses a recorded decision — the database is the record, the keyboard is a view of
it.

Pressing the same button twice is a no-op with an acknowledgement. Pressing a
different one records the change: a mis-tap the owner cannot correct would
quietly corrupt the real-vs-hypothetical split for good.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from sentinel.bot.cards import DECISION_LABEL, decision_ack_card
from sentinel.bot.context import BotContext
from sentinel.bot.keyboards import (
    DecisionCallback,
    ResumeCallback,
    decision_keyboard,
    decision_keyboard_with_manage,
)
from sentinel.bot.models import MessageKind, SignalDecision
from sentinel.core.logging import get_logger
from sentinel.risk.models import PauseState

#: The ``telegram_messages.event_key`` the decision acknowledgement claims.
ACK_KEY = "decision_ack"

log = get_logger(__name__)

callbacks_router = Router(name="callbacks")


@callbacks_router.callback_query(DecisionCallback.filter())
async def decision(query: CallbackQuery, callback_data: DecisionCallback, ctx: BotContext) -> None:
    """Record ✅ Taken / 👀 Watching / ❌ Skip and reflect it on the keyboard."""
    assert query.from_user is not None
    chosen = callback_data.decision

    async with ctx.database.session() as session:
        result = await ctx.repositories.signals(session).record_decision(
            callback_data.signal_id,
            chosen,
            at=ctx.clock.now(),
            user_id=query.from_user.id,
        )
        if result is None:
            await query.answer("That signal is no longer in the database.", show_alert=True)
            return
        _row, changed = result
        await session.commit()

    log.info(
        "bot.decision_recorded",
        signal_id=str(callback_data.signal_id),
        decision=chosen.value,
        user_id=query.from_user.id,
        # A re-press writes nothing. Without this flag the audit trail shows two
        # "recorded" lines for one decision, which M9 would have to disentangle.
        changed=changed,
    )
    await query.answer(f"Recorded: {DECISION_LABEL[chosen]}")

    # Owner requirement (M7): a visible confirmation, not only the keyboard
    # marker. The marker is easy to miss on a phone, and this is the input that
    # decides whether an outcome lands in the real statistics or the hypothetical
    # ones — the single most consequential tap in the system.
    await _acknowledge(query, ctx, callback_data.signal_id, chosen, _row)

    if not changed and _keyboard_already_shows(query, chosen):
        return
    try:
        await query.message.edit_reply_markup(  # type: ignore[union-attr]
            reply_markup=_keyboard_for(callback_data.signal_id, chosen, _row)
        )
    except (TelegramBadRequest, AttributeError):
        # The decision is already committed. An un-editable message — too old, or
        # unchanged markup — must not look like a failure to record it.
        log.info("bot.keyboard_edit_skipped", signal_id=str(callback_data.signal_id))


def _keyboard_for(signal_id: UUID, chosen: SignalDecision, row: Any) -> InlineKeyboardMarkup:
    """§2's second row appears once the tracker has seen a fill, and not before."""
    if getattr(row, "filled_qty", Decimal(0)) > 0:
        return decision_keyboard_with_manage(signal_id, chosen)
    return decision_keyboard(signal_id, chosen)


async def _acknowledge(
    query: CallbackQuery,
    ctx: BotContext,
    signal_id: UUID,
    chosen: SignalDecision,
    row: Any,
) -> None:
    """One acknowledgement per signal per chat, **edited** when the decision changes.

    Edited rather than re-posted because the buttons stay live so a mis-tap can be
    corrected (M6 decision 3): a fresh reply per press would bury the card under
    acknowledgements, and leaving the first one in place would leave a stale
    "marked Taken" under a signal the owner later skipped — the one thing that
    must never be wrong, since it drives the real-vs-hypothetical split.

    The claim is the same ``telegram_messages`` row the publisher and the notifier
    use, with ``event_key='decision_ack'``, so a restart never double-posts it.
    """
    chat_id = query.message.chat.id  # type: ignore[union-attr]
    text = decision_ack_card(chosen, getattr(row, "number", 0), getattr(row, "symbol", ""))

    async with ctx.database.session() as session:
        messages = ctx.repositories.messages(session)
        existing = await messages.get(signal_id, MessageKind.UPDATE, chat_id, ACK_KEY)
        claimed = (
            False
            if existing is not None
            else await messages.claim(
                signal_id,
                MessageKind.UPDATE,
                chat_id,
                at=ctx.clock.now(),
                event_key=ACK_KEY,
            )
        )
        await session.commit()

    if existing is not None and existing.message_id is not None:
        try:
            await query.bot.edit_message_text(  # type: ignore[union-attr]
                chat_id=chat_id, message_id=existing.message_id, text=text
            )
        except (TelegramBadRequest, AttributeError):
            # Unchanged text (the same button pressed twice) or a message too old
            # to edit. The database already holds the decision either way.
            log.info("bot.decision_ack_edit_skipped", signal_id=str(signal_id))
        return

    if not claimed:  # pragma: no cover — a claim with no id is the crash window
        return

    try:
        sent = await query.message.reply(text)  # type: ignore[union-attr]
    except (TelegramBadRequest, AttributeError) as exc:
        async with ctx.database.session() as session:
            await ctx.repositories.messages(session).fail(
                signal_id, MessageKind.UPDATE, chat_id, error=str(exc), event_key=ACK_KEY
            )
            await session.commit()
        return

    async with ctx.database.session() as session:
        await ctx.repositories.messages(session).confirm(
            signal_id,
            MessageKind.UPDATE,
            chat_id,
            message_id=int(sent.message_id),
            at=ctx.clock.now(),
            event_key=ACK_KEY,
        )
        await session.commit()


def _keyboard_already_shows(query: CallbackQuery, chosen: SignalDecision) -> bool:
    """Telegram rejects an edit that changes nothing; don't make a pointless call."""
    markup = query.message.reply_markup if query.message is not None else None  # type: ignore[union-attr]
    if markup is None:
        return False
    return any(
        button.text.startswith("»") and DECISION_LABEL[chosen] in button.text
        for row in markup.inline_keyboard
        for button in row
    )


@callbacks_router.callback_query(ResumeCallback.filter())
async def resume_confirmation(
    query: CallbackQuery, callback_data: ResumeCallback, ctx: BotContext
) -> None:
    """§3 — "resume from loss-limit pause requires confirming button "Yes, resume"."""
    assert query.from_user is not None
    if not callback_data.confirm:
        await query.answer("Still paused.")
        await _replace(query, "⏸️ Still paused. The loss-limit rail stays in place.")
        return

    async with ctx.database.session() as session:
        await ctx.repositories.risk_state(session).save(PauseState())
        await session.commit()

    log.warning(
        "bot.loss_limit_pause_overridden",
        user_id=query.from_user.id,
        detail="owner confirmed resume from a daily-loss-limit pause",
    )
    await query.answer("Resumed.")
    await _replace(
        query,
        "▶️ <b>Resumed</b> — the daily loss-limit pause was overridden on your "
        "confirmation. New signals will be gated normally again.",
    )


async def _replace(query: CallbackQuery, text: str) -> None:
    try:
        await query.message.edit_text(text, reply_markup=None)  # type: ignore[union-attr]
    except (TelegramBadRequest, AttributeError):  # pragma: no cover — cosmetic only
        log.info("bot.message_edit_skipped")


__all__ = ["ACK_KEY", "callbacks_router"]
