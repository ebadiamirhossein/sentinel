"""🔚 Closed manually and ✏️ Note — the prompt, the reply, and the R it records.

specs/TELEGRAM_UX.md §2: "manual close asks for exit price to record honest
realized R". The interesting property is not the arithmetic (that is
``risk/accounting.py``'s, tested there) but that the *question survives a
restart*: no FSM, no in-memory conversation state, just a prompt whose message id
is claimed in ``telegram_messages`` and recovered from the reply.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.context import BotContext
from sentinel.bot.handlers import replies
from sentinel.bot.keyboards import ManageAction, ManageCallback
from sentinel.bot.models import MessageKind, SignalStatus
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Secrets, Settings, load_config
from tests.bot_double import FakeDatabase, FakeStore, _SignalRow, fake_repositories
from tests.risk_double import approved_plan

SIGNAL_ID = UUID("22222222-2222-2222-2222-222222222222")
CHAT_ID = 4242
NOW = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
PROMPT_ID = 777


class _Chat:
    id = CHAT_ID


class _Sent:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeMessage:
    def __init__(self, text: str | None = None, reply_to: int | None = None) -> None:
        self.text = text
        self.chat = _Chat()
        self.replies: list[str] = []
        self.reply_to_message = _Sent(reply_to) if reply_to is not None else None

    async def reply(self, text: str, reply_markup: Any = None) -> _Sent:
        self.replies.append(text)
        return _Sent(PROMPT_ID)

    @property
    def last(self) -> str:
        return self.replies[-1]


class FakeQuery:
    def __init__(self) -> None:
        self.message = FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    plan = approved_plan(load_config())
    row = _SignalRow(SIGNAL_ID, plan.plan_id, number=7)
    row.plan = plan.model_dump(mode="json")
    row.filled_qty = Decimal("18.30")
    row.status = SignalStatus.PARTIALLY_FILLED.value
    store.signals[SIGNAL_ID] = row
    return store


@pytest.fixture
def ctx(store: FakeStore, tz: ZoneInfo) -> BotContext:
    return BotContext(
        settings=Settings(secrets=Secrets(_env_file=None), config=load_config()),
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        tz=tz,
        repositories=fake_repositories(),
    )


def fills_and_exits(store: FakeStore) -> None:
    """The tracker's rung-1 fill, as the manual close will read it back."""
    store.fills[(SIGNAL_ID, 0)] = {
        "price": Decimal("83.10"),
        "qty": Decimal("18.30"),
    }


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #


async def test_the_close_button_asks_for_the_price_it_needs(
    ctx: BotContext, store: FakeStore
) -> None:
    query = FakeQuery()
    await replies.manage(
        query,
        ManageCallback(signal_id=SIGNAL_ID, action=ManageAction.CLOSE),
        ctx,
    )
    assert "reply to this message with the price you were actually filled at" in (
        query.message.last.lower()
    )
    # And the question is now durable: the prompt's message id is claimed.
    key = (SIGNAL_ID, MessageKind.UPDATE.value, CHAT_ID, "manage_prompt:close")
    assert store.messages[key].message_id == PROMPT_ID


async def test_the_close_button_refuses_a_signal_that_has_not_filled(
    ctx: BotContext, store: FakeStore
) -> None:
    """§2 puts the row on a signal "after entry fills". Asking for an exit price on
    a position that was never opened would record a trade that never happened."""
    store.signals[SIGNAL_ID].filled_qty = Decimal("0")
    query = FakeQuery()
    await replies.manage(
        query,
        ManageCallback(signal_id=SIGNAL_ID, action=ManageAction.CLOSE),
        ctx,
    )
    assert query.answers == ["Nothing has filled on this signal yet."]
    assert query.message.replies == []


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #


async def test_a_reply_to_the_prompt_records_the_close_and_its_realized_r(
    ctx: BotContext, store: FakeStore
) -> None:
    """Rung 1 filled at 83.10; the owner got out at 84.00.

    P&L  (84.00 - 83.10) x 18.30 = 16.47 USDT
    R    16.47 / 86.9475         = 0.1894...  -> 0.19R
    """
    fills_and_exits(store)
    query = FakeQuery()
    await replies.manage(
        query,
        ManageCallback(signal_id=SIGNAL_ID, action=ManageAction.CLOSE),
        ctx,
    )

    answer = FakeMessage(text="84.00", reply_to=PROMPT_ID)
    await replies.answered(answer, ctx)

    assert "0.19R" in answer.last
    row = store.signals[SIGNAL_ID]
    assert row.status == SignalStatus.CLOSED.value
    assert row.realized_r == Decimal("0.19")
    assert store.exits[(SIGNAL_ID, "MANUAL")]["price"] == Decimal("84.00")


async def test_a_reply_that_is_not_a_price_is_rejected_rather_than_stored(
    ctx: BotContext, store: FakeStore
) -> None:
    fills_and_exits(store)
    query = FakeQuery()
    await replies.manage(
        query,
        ManageCallback(signal_id=SIGNAL_ID, action=ManageAction.CLOSE),
        ctx,
    )

    answer = FakeMessage(text="about eighty four", reply_to=PROMPT_ID)
    await replies.answered(answer, ctx)

    assert "not a price" in answer.last
    assert store.signals[SIGNAL_ID].status == SignalStatus.PARTIALLY_FILLED.value


async def test_nan_is_refused_by_name(ctx: BotContext, store: FakeStore) -> None:
    """``Decimal("nan")`` parses cleanly and would poison every downstream figure —
    M6 §7 found exactly this in ``/capital``, and this input feeds the same math."""
    fills_and_exits(store)
    query = FakeQuery()
    await replies.manage(
        query,
        ManageCallback(signal_id=SIGNAL_ID, action=ManageAction.CLOSE),
        ctx,
    )
    for hostile in ("nan", "inf", "-5", "0"):
        answer = FakeMessage(text=hostile, reply_to=PROMPT_ID)
        await replies.answered(answer, ctx)
        assert "not a price" in answer.last, hostile
    assert store.signals[SIGNAL_ID].realized_r is None


async def test_a_note_is_stored_against_the_signal(ctx: BotContext, store: FakeStore) -> None:
    query = FakeQuery()
    await replies.manage(
        query,
        ManageCallback(signal_id=SIGNAL_ID, action=ManageAction.NOTE),
        ctx,
    )
    answer = FakeMessage(text="entered late, chased the retest", reply_to=PROMPT_ID)
    await replies.answered(answer, ctx)

    assert "Noted:" in answer.last
    notes = [row for key, row in store.events.items() if key[0] == SIGNAL_ID]
    assert notes and notes[0]["detail"] == "entered late, chased the retest"


async def test_an_unrelated_reply_is_ignored_in_silence(ctx: BotContext, store: FakeStore) -> None:
    """Every message that is a reply reaches this handler. One that answers no
    prompt of ours must produce nothing at all — not an error, not a hint."""
    answer = FakeMessage(text="84.00", reply_to=99999)
    await replies.answered(answer, ctx)
    assert answer.replies == []


async def test_the_question_survives_a_restart(ctx: BotContext, store: FakeStore) -> None:
    """The reason there is no FSM. The prompt was claimed in the database, so a
    brand-new context — a restarted process — still knows what it asked."""
    fills_and_exits(store)
    query = FakeQuery()
    await replies.manage(
        query,
        ManageCallback(signal_id=SIGNAL_ID, action=ManageAction.CLOSE),
        ctx,
    )

    restarted = BotContext(
        settings=ctx.settings,
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        tz=ctx.tz,
        repositories=fake_repositories(),
    )
    answer = FakeMessage(text="84.00", reply_to=PROMPT_ID)
    await replies.answered(answer, restarted)

    assert store.signals[SIGNAL_ID].status == SignalStatus.CLOSED.value
