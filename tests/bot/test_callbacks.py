"""Inline buttons — specs/TELEGRAM_UX.md §2.

Taken / Watching / Skip is the most consequential input the owner gives this
system: at M7 it decides whether an outcome counts toward the **real** statistics
or the hypothetical ones, and whether the signal consumes the open-risk budget.
So the decision is persisted before the keyboard is touched, and a keyboard that
cannot be edited never costs a recorded decision.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest

from sentinel.bot.context import BotContext
from sentinel.bot.handlers import callbacks
from sentinel.bot.keyboards import DecisionCallback, ResumeCallback, decision_keyboard
from sentinel.bot.models import SignalDecision
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Secrets, Settings, load_config
from sentinel.risk.models import PauseReason, PauseState
from tests.bot_double import FakeDatabase, FakeStore, _SignalRow, fake_repositories

OWNER = 111
SIGNAL_ID = UUID(int=1)


class _Chat:
    def __init__(self, chat_id: int = OWNER) -> None:
        self.id = chat_id


class FakeQueryMessage:
    def __init__(self, markup: Any = None) -> None:
        self.reply_markup = markup
        self.chat = _Chat()
        self.edits: list[Any] = []
        self.texts: list[str] = []
        #: M7: the decision acknowledgement is a *reply* under the card.
        self.replies: list[str] = []
        self.fail: Exception | None = None
        self.reply_fail: Exception | None = None

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        if self.fail is not None:
            raise self.fail
        self.edits.append(reply_markup)
        self.reply_markup = reply_markup

    async def edit_text(self, text: str, reply_markup: Any = None) -> None:
        self.texts.append(text)

    async def reply(self, text: str, reply_markup: Any = None) -> Any:
        if self.reply_fail is not None:
            raise self.reply_fail
        self.replies.append(text)
        return _Sent(500 + len(self.replies))


class _Sent:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeQueryBot:
    """The one method the acknowledgement edit needs."""

    def __init__(self) -> None:
        self.edits: list[tuple[int, str]] = []
        self.fail: Exception | None = None

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> Any:
        if self.fail is not None:
            raise self.fail
        self.edits.append((message_id, text))
        return _Sent(message_id)


class FakeQuery:
    def __init__(self, message: FakeQueryMessage | None = None) -> None:
        self.from_user = _User(OWNER)
        self.message = message or FakeQueryMessage()
        self.bot = FakeQueryBot()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    store.signals[SIGNAL_ID] = _SignalRow(SIGNAL_ID, uuid4(), number=1)
    return store


@pytest.fixture
def ctx(store: FakeStore, tz: ZoneInfo, clock: FrozenClock) -> BotContext:
    return BotContext(
        settings=Settings(secrets=Secrets(_env_file=None), config=load_config()),
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=clock,
        tz=tz,
        repositories=fake_repositories(),
    )


@pytest.mark.parametrize("decision", list(SignalDecision))
async def test_a_button_press_is_persisted(
    ctx: BotContext, store: FakeStore, decision: SignalDecision
) -> None:
    query = FakeQuery()
    await callbacks.decision(
        query,
        DecisionCallback(signal_id=SIGNAL_ID, decision=decision),
        ctx,
    )

    row = store.signals[SIGNAL_ID]
    assert row.decision == decision.value
    assert row.decided_by_user_id == OWNER
    assert row.decided_at == ctx.clock.now()
    assert query.answers, "Telegram spins forever if a callback is never answered"


async def test_the_keyboard_reflects_the_decision(ctx: BotContext) -> None:
    query = FakeQuery()
    await callbacks.decision(
        query,
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.TAKEN),
        ctx,
    )
    marked = [
        button.text
        for row in query.message.edits[-1].inline_keyboard
        for button in row
        if button.text.startswith("»")
    ]
    assert marked == ["» ✅ Taken «"]


async def test_pressing_the_same_button_twice_changes_nothing(
    ctx: BotContext, store: FakeStore
) -> None:
    callback = DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.TAKEN)
    first = FakeQuery()
    await callbacks.decision(first, callback, ctx)
    decided_at = store.signals[SIGNAL_ID].decided_at

    second = FakeQuery(FakeQueryMessage(decision_keyboard(SIGNAL_ID, SignalDecision.TAKEN)))
    await callbacks.decision(second, callback, ctx)

    assert store.signals[SIGNAL_ID].decided_at == decided_at
    assert second.message.edits == [], "Telegram rejects an edit that changes nothing"
    assert second.answers


async def test_a_mistap_can_be_corrected(ctx: BotContext, store: FakeStore) -> None:
    """An uncorrectable mis-tap would permanently corrupt the real-vs-hypothetical
    split that every M7 statistic rests on."""
    query = FakeQuery()
    await callbacks.decision(
        query,
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.TAKEN),
        ctx,
    )
    await callbacks.decision(
        FakeQuery(),
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.SKIPPED),
        ctx,
    )
    assert store.signals[SIGNAL_ID].decision == SignalDecision.SKIPPED.value


async def test_a_failed_keyboard_edit_does_not_lose_the_decision(
    ctx: BotContext, store: FakeStore
) -> None:
    """The database is the record; the keyboard is a view of it."""
    message = FakeQueryMessage()
    message.fail = TelegramBadRequest(
        method=None,  # type: ignore[arg-type]
        message="message is too old",
    )
    query = FakeQuery(message)

    await callbacks.decision(
        query,
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.WATCHING),
        ctx,
    )
    assert store.signals[SIGNAL_ID].decision == SignalDecision.WATCHING.value


async def test_a_button_for_an_unknown_signal_says_so(ctx: BotContext) -> None:
    query = FakeQuery()
    await callbacks.decision(
        query,
        DecisionCallback(signal_id=UUID(int=99), decision=SignalDecision.TAKEN),
        ctx,
    )
    assert "no longer in the database" in query.answers[0]


async def test_confirming_a_loss_limit_resume_lifts_the_pause(
    ctx: BotContext, store: FakeStore
) -> None:
    store.pause = PauseState(paused=True, reason=PauseReason.DAILY_LOSS_LIMIT)
    query = FakeQuery()

    await callbacks.resume_confirmation(query, ResumeCallback(confirm=True), ctx)

    assert store.pause.paused is False
    assert "Resumed" in query.message.texts[-1]


async def test_declining_the_confirmation_keeps_the_pause(
    ctx: BotContext, store: FakeStore
) -> None:
    store.pause = PauseState(paused=True, reason=PauseReason.DAILY_LOSS_LIMIT)
    query = FakeQuery()

    await callbacks.resume_confirmation(query, ResumeCallback(confirm=False), ctx)

    assert store.pause.paused is True
    assert "Still paused" in query.message.texts[-1]


def test_callback_payloads_fit_telegram_s_64_byte_limit() -> None:
    """A payload that overflows is silently rejected by Telegram, not reported."""
    packed = DecisionCallback(signal_id=uuid4(), decision=SignalDecision.WATCHING).pack()
    assert len(packed.encode()) <= 64
    assert len(ResumeCallback(confirm=True).pack().encode()) <= 64


# --------------------------------------------------------------------------- #
# The decision acknowledgement (owner requirement, M7)
# --------------------------------------------------------------------------- #


async def test_a_decision_posts_a_visible_confirmation(ctx: BotContext) -> None:
    """The keyboard marker is easy to miss on a phone, and this is the tap that
    decides whether an outcome counts as real or hypothetical."""
    query = FakeQuery()
    await callbacks.decision(
        query,
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.TAKEN),
        ctx,
    )
    assert query.message.replies, "a decision must say so in the thread, not only on the button"
    assert "marked <b>Taken</b>" in query.message.replies[0]


async def test_the_confirmation_is_edited_when_the_decision_changes(
    ctx: BotContext, store: FakeStore
) -> None:
    """One acknowledgement per signal, kept current. A stale "marked Taken" under a
    signal the owner later skipped would be the one thing that must never be
    wrong — it drives the real-vs-hypothetical split every statistic rests on."""
    query = FakeQuery()
    for decision in (SignalDecision.TAKEN, SignalDecision.SKIPPED):
        await callbacks.decision(
            query,
            DecisionCallback(signal_id=SIGNAL_ID, decision=decision),
            ctx,
        )

    assert len(query.message.replies) == 1, "no second reply — the thread stays readable"
    assert query.bot.edits, "the existing acknowledgement must be updated"
    assert "marked <b>Skipped</b>" in query.bot.edits[-1][1]


async def test_the_confirmation_claim_survives_a_restart(
    ctx: BotContext, store: FakeStore, tz: ZoneInfo, clock: FrozenClock
) -> None:
    """It is claimed in ``telegram_messages`` like every other message (§6), so a
    restarted process edits the acknowledgement rather than posting a second."""
    await callbacks.decision(
        FakeQuery(),
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.TAKEN),
        ctx,
    )
    restarted = BotContext(
        settings=ctx.settings,
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=clock,
        tz=tz,
        repositories=fake_repositories(),
    )
    after = FakeQuery()
    await callbacks.decision(
        after,
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.WATCHING),
        restarted,
    )
    assert after.message.replies == [], "a restart must not post a second acknowledgement"
    assert after.bot.edits


async def test_a_failed_confirmation_never_loses_the_decision(
    ctx: BotContext, store: FakeStore
) -> None:
    """The database is the record; the message is a view of it (M6 decision 4)."""
    query = FakeQuery()
    query.message.reply_fail = TelegramBadRequest(method=Mock(), message="too old")

    await callbacks.decision(
        query,
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.TAKEN),
        ctx,
    )
    assert store.signals[SIGNAL_ID].decision == SignalDecision.TAKEN.value


async def test_the_manage_row_appears_only_once_something_has_filled(
    ctx: BotContext, store: FakeStore
) -> None:
    """§2 puts the row on a signal "after entry fills" — which is exactly why M6
    deferred it, and exactly what the tracker now makes knowable."""
    unfilled = FakeQuery()
    await callbacks.decision(
        unfilled,
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.TAKEN),
        ctx,
    )
    assert len(unfilled.message.edits[-1].inline_keyboard) == 1

    store.signals[SIGNAL_ID].filled_qty = Decimal("18.30")
    filled = FakeQuery()
    await callbacks.decision(
        filled,
        DecisionCallback(signal_id=SIGNAL_ID, decision=SignalDecision.WATCHING),
        ctx,
    )
    rows = filled.message.edits[-1].inline_keyboard
    assert len(rows) == 2
    assert [button.text for button in rows[1]] == ["🔚 Closed manually", "✏️ Note"]
