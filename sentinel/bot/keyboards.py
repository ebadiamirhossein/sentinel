"""Inline keyboards and callback payloads (specs/TELEGRAM_UX.md §2).

Callback data is typed rather than string-parsed: aiogram's ``CallbackData``
serializes to the 64-byte payload Telegram allows and rejects anything that does
not fit the declared shape, so a malformed or forged callback never reaches a
handler that would write to the database.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sentinel.bot.cards import DECISION_LABEL
from sentinel.bot.models import SignalDecision

#: §2's three buttons, in the spec's order.
DECISION_ORDER = (SignalDecision.TAKEN, SignalDecision.WATCHING, SignalDecision.SKIPPED)

BUTTON_LABEL = {
    SignalDecision.TAKEN: "✅ Taken",
    SignalDecision.WATCHING: "👀 Watching",
    SignalDecision.SKIPPED: "❌ Skip",
}


class DecisionCallback(CallbackData, prefix="sig"):
    """Which signal, and what the owner decided about it."""

    signal_id: UUID
    decision: SignalDecision


class ResumeCallback(CallbackData, prefix="resume"):
    """§3 — resuming from a loss-limit pause requires an explicit confirmation."""

    confirm: bool


class ManageAction(StrEnum):
    """§2's second row, deferred at M6 and shipped at M7."""

    CLOSE = "close"
    NOTE = "note"


class ManageCallback(CallbackData, prefix="mng"):
    """§2: "After entry fills, ACTIVE signals gain a second row".

    M6 deferred this because a fill is detected by the tracker, and there was no
    tracker: a button that could never appear, or one that appeared on a signal
    the system could not tell was filled, were both worse than the gap.
    """

    signal_id: UUID
    action: ManageAction


def decision_keyboard(
    signal_id: UUID, chosen: SignalDecision | None = None
) -> InlineKeyboardMarkup:
    """The card's button row; the chosen option is marked once one is pressed.

    The buttons stay live after a decision so the owner can correct a mis-tap —
    marking a signal Taken by accident and being unable to undo it would corrupt
    the real-vs-hypothetical split that M7's statistics depend on.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"» {DECISION_LABEL[decision]} «"
                        if decision is chosen
                        else BUTTON_LABEL[decision]
                    ),
                    callback_data=DecisionCallback(signal_id=signal_id, decision=decision).pack(),
                )
                for decision in DECISION_ORDER
            ]
        ]
    )


def decision_keyboard_with_manage(
    signal_id: UUID, chosen: SignalDecision | None = None
) -> InlineKeyboardMarkup:
    """The decision row plus §2's manage row, for a signal that has filled.

    A second row rather than a replacement: the decision must stay correctable
    after a fill, because a mis-tap that cannot be undone corrupts the
    real-vs-hypothetical split permanently (M6 decision 3), and a filled signal is
    exactly when the owner is most likely to be tapping quickly.
    """
    base = decision_keyboard(signal_id, chosen)
    return InlineKeyboardMarkup(inline_keyboard=[*base.inline_keyboard, manage_row(signal_id)])


def manage_row(signal_id: UUID) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text="🔚 Closed manually",
            callback_data=ManageCallback(signal_id=signal_id, action=ManageAction.CLOSE).pack(),
        ),
        InlineKeyboardButton(
            text="✏️ Note",
            callback_data=ManageCallback(signal_id=signal_id, action=ManageAction.NOTE).pack(),
        ),
    ]


def resume_keyboard() -> InlineKeyboardMarkup:
    """§3's "Yes, resume" confirmation for a loss-limit pause."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Yes, resume", callback_data=ResumeCallback(confirm=True).pack()
                ),
                InlineKeyboardButton(
                    text="↩️ Stay paused", callback_data=ResumeCallback(confirm=False).pack()
                ),
            ]
        ]
    )


__all__ = [
    "BUTTON_LABEL",
    "DECISION_ORDER",
    "DecisionCallback",
    "ManageAction",
    "ManageCallback",
    "ResumeCallback",
    "decision_keyboard",
    "decision_keyboard_with_manage",
    "manage_row",
    "resume_keyboard",
]
