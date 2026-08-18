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

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from sentinel.bot.cards import DECISION_LABEL
from sentinel.bot.context import BotContext
from sentinel.bot.keyboards import DecisionCallback, ResumeCallback, decision_keyboard
from sentinel.bot.models import SignalDecision
from sentinel.core.logging import get_logger
from sentinel.risk.models import PauseState

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
    )
    await query.answer(f"Recorded: {DECISION_LABEL[chosen]}")

    if not changed and _keyboard_already_shows(query, chosen):
        return
    try:
        await query.message.edit_reply_markup(  # type: ignore[union-attr]
            reply_markup=decision_keyboard(callback_data.signal_id, chosen)
        )
    except (TelegramBadRequest, AttributeError):
        # The decision is already committed. An un-editable message — too old, or
        # unchanged markup — must not look like a failure to record it.
        log.info("bot.keyboard_edit_skipped", signal_id=str(callback_data.signal_id))


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


__all__ = ["callbacks_router"]
