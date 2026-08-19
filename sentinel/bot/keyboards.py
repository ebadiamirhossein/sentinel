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
from sentinel.bot.models import ACK_VERSION, SignalDecision

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


#: M8.1's callback prefixes. ``ACK_PREFIX`` is a module constant because
#: ``auth.py`` has to recognise the acknowledgement button *before* routing — it is
#: the one press that must work while everything else is still gated.
ACK_PREFIX = "ack"
ADMIN_PREFIX = "adm"
LEAVE_PREFIX = "leave"


class AckCallback(CallbackData, prefix=ACK_PREFIX):
    """The first-run acknowledgement (specs/TELEGRAM_UX.md §7).

    Carries the wording's version, so what is recorded is what was on screen. A
    stale button from a superseded note therefore records the old version and the
    current one is asked for again, rather than a bare timestamp implying consent to
    words the user never saw.
    """

    version: str


class AdminAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class AdminCallback(CallbackData, prefix=ADMIN_PREFIX):
    """The owner's Approve/Reject buttons on a registration request.

    The handler re-checks the role against the database rather than trusting this
    payload: a callback is client-supplied, and a forwarded request card carries its
    buttons to whoever it was forwarded to.
    """

    user_id: int
    action: AdminAction


class LeaveCallback(CallbackData, prefix=LEAVE_PREFIX):
    """``/leave``'s confirmation. Leaving is one tap away, but not zero."""

    confirm: bool


def acknowledge_keyboard(version: str | None = None) -> InlineKeyboardMarkup:
    """One button under the first-run note. No "decline" — that is ``/leave``."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ I understand",
                    callback_data=AckCallback(version=version or ACK_VERSION).pack(),
                )
            ]
        ]
    )


def approval_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """§7's Approve/Reject row on the request that reaches the owner."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Approve",
                    callback_data=AdminCallback(user_id=user_id, action=AdminAction.APPROVE).pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Reject",
                    callback_data=AdminCallback(user_id=user_id, action=AdminAction.REJECT).pack(),
                ),
            ]
        ]
    )


def leave_keyboard() -> InlineKeyboardMarkup:
    """``/leave``'s confirmation, worded so neither button is the accidental one."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚪 Yes, remove me", callback_data=LeaveCallback(confirm=True).pack()
                ),
                InlineKeyboardButton(
                    text="↩️ Stay", callback_data=LeaveCallback(confirm=False).pack()
                ),
            ]
        ]
    )


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
    "ACK_PREFIX",
    "ADMIN_PREFIX",
    "BUTTON_LABEL",
    "DECISION_ORDER",
    "LEAVE_PREFIX",
    "AckCallback",
    "AdminAction",
    "AdminCallback",
    "DecisionCallback",
    "LeaveCallback",
    "ManageAction",
    "ManageCallback",
    "ResumeCallback",
    "acknowledge_keyboard",
    "approval_keyboard",
    "decision_keyboard",
    "decision_keyboard_with_manage",
    "leave_keyboard",
    "manage_row",
    "resume_keyboard",
]
