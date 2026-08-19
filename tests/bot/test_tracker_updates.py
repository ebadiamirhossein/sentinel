"""§4's threaded replies, the decision acknowledgement, and the manage row.

Three things M6 could not build and M7 owes: a reply per tracker event, a visible
confirmation when a decision button is pressed (owner requirement — the keyboard
marker is easy to miss on a phone), and §2's second row, which M6 deferred because
a fill is detected by a tracker that did not exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from sentinel.bot.cards import decision_ack_card, tracker_update_card
from sentinel.bot.keyboards import (
    ManageAction,
    ManageCallback,
    decision_keyboard_with_manage,
    manage_row,
)
from sentinel.bot.models import MessageKind, SignalDecision
from sentinel.bot.notifier import TrackerNotifier
from sentinel.bot.views import TrackerEventView
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from tests.bot_double import FakeBot, FakeDatabase, FakeStore, _MessageRow, _SignalRow

CHAT_ID = 4242
SIGNAL_ID = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)


def event(kind: str, **overrides: Any) -> Any:
    base: dict[str, Any] = {
        "signal_id": SIGNAL_ID,
        "event_key": kind.lower(),
        "kind": kind,
        "at": NOW,
        "price": Decimal("83.10"),
        "realized_r": None,
        "realized_eur": None,
        "payload": {},
        "detail": "",
    }
    base.update(overrides)
    return type("Row", (), base)()


def view(kind: str, **overrides: Any) -> TrackerEventView:
    base: dict[str, Any] = {"kind": kind, "symbol": "SOLUSDT", "number": 7}
    base.update(overrides)
    return TrackerEventView(**base)


# --------------------------------------------------------------------------- #
# The §4 wording
# --------------------------------------------------------------------------- #


def test_a_fill_reads_like_the_spec() -> None:
    """§4: "📥 Entry 1 filled @ 83.10 (40%)"."""
    text = tracker_update_card(
        view("ENTRY_FILLED", price="83.10", payload={"rung": "1", "weight_pct": "40"})
    )
    assert "📥" in text and "Entry 1 filled @ 83.10 (40% of risk)" in text


def test_a_completed_ladder_reports_the_average_actually_held() -> None:
    """§4: "📥 Ladder complete, avg 82.68"."""
    text = tracker_update_card(view("LADDER_COMPLETE", price="82.55"))
    assert "Ladder complete, avg 82.55" in text


def test_a_target_reports_the_banked_r_and_the_breakeven_move() -> None:
    """§4: "🎯 TP1 hit @ 84.90 → close 40%, stop moved to BE (per plan)"."""
    text = tracker_update_card(
        view(
            "TP_HIT",
            price="85.20",
            realized_r="0.78",
            payload={
                "target": "1",
                "breakeven": "82.55",
                "management": "TP1: close 40%, move stop to breakeven.",
            },
        )
    )
    assert "🎯" in text and "TP1 hit @ 85.20 → 0.78R banked" in text
    assert "Stop moved to breakeven 82.55 (per plan)" in text
    assert "TP1: close 40%" in text


def test_a_stop_out_explains_a_partial_ladder() -> None:
    """§4: "🛑 Stopped @ 81.20 -> -0.72R (partial ladder: only rungs 1-2 filled)".

    The parenthetical is why the loss is not a full R, stated on the message rather
    than left for the owner to reconstruct at 3am.
    """
    text = tracker_update_card(
        view(
            "STOPPED",
            price="81.20",
            realized_r="-0.75",
            realized_eur="-56.23",
            payload={"partial_ladder": "1, 2"},
        )
    )
    assert "🛑" in text
    assert "Stopped @ 81.20 → -0.75R (partial ladder: only rung(s) 1, 2 filled)" in text
    assert "€-56.23" in text


def test_a_full_ladder_stop_out_says_nothing_about_partials() -> None:
    text = tracker_update_card(
        view("STOPPED", price="81.20", realized_r="-1.00", realized_eur="-74.98", payload={})
    )
    assert "partial ladder" not in text


def test_invalidation_and_expiry_read_like_the_spec() -> None:
    invalidated = tracker_update_card(
        view("INVALIDATED", payload={"close": "81.05", "level": "81.40"})
    )
    assert "❌" in invalidated
    assert "Invalidation triggered (close 81.05 vs 81.40) before entry" in invalidated
    assert "signal cancelled" in invalidated

    assert "⌛" in tracker_update_card(view("EXPIRED"))
    assert "Expired unfilled" in tracker_update_card(view("EXPIRED"))


# --------------------------------------------------------------------------- #
# Delivery: once per event, as a reply, and never for a rehearsal
# --------------------------------------------------------------------------- #


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    # chat id == user id on Telegram, which is the identity M8.1's notifier
    # relies on: the queue is per chat *and* per owner of the signal.
    store.signals[SIGNAL_ID] = _SignalRow(SIGNAL_ID, uuid4(), number=7, user_id=CHAT_ID)
    store.messages[(SIGNAL_ID, MessageKind.CARD.value, CHAT_ID, "")] = _MessageRow(
        signal_id=SIGNAL_ID,
        kind=MessageKind.CARD.value,
        chat_id=CHAT_ID,
        status="SENT",
        message_id=900,
    )
    return store


def notifier(
    store: FakeStore, bot: FakeBot, config: AppConfig, events: list[Any]
) -> TrackerNotifier:
    from tests.bot_double import FakeMessageStore

    class Events:
        def __init__(self, session: Any) -> None:
            self._session = session

        async def unposted(self, chat_id: int, *, user_id: int, limit: int = 100) -> list[Any]:
            posted = {
                key[3]
                for key in store.messages
                if key[1] == MessageKind.UPDATE.value and key[2] == chat_id
            }
            mine = {signal_id for signal_id, row in store.signals.items() if row.user_id == user_id}
            return [
                item for item in events if item.event_key not in posted and item.signal_id in mine
            ]

    class Signals:
        def __init__(self, session: Any) -> None:
            self._session = session

        async def get(self, signal_id: UUID) -> Any:
            return store.signals.get(signal_id)

    return TrackerNotifier(
        FakeDatabase(store),  # type: ignore[arg-type]
        bot,
        chat_ids=(CHAT_ID,),
        telegram=config.telegram,
        clock=FrozenClock(NOW),
        messages=FakeMessageStore,
        events=Events,  # type: ignore[arg-type]
        signals=Signals,  # type: ignore[arg-type]
    )


async def test_an_event_is_posted_as_a_reply_to_the_card(
    store: FakeStore, fake_bot: FakeBot, bot_config: AppConfig
) -> None:
    """§4: "Tracker notifications (replies to the original card)"."""
    events = [event("ENTRY_FILLED", payload={"rung": "1", "weight_pct": "40"})]
    sent = await notifier(store, fake_bot, bot_config, events).deliver()

    assert sent == 1
    call = fake_bot.of("send_message")[0]
    assert call.kwargs["reply_to_message_id"] == 900, "the update must land in the thread"
    assert "Entry 1 filled" in call.kwargs["text"]


async def test_an_event_is_never_posted_twice(
    store: FakeStore, fake_bot: FakeBot, bot_config: AppConfig
) -> None:
    """The tracker re-derives the same events from the same candles on every tick.
    The claim on ``(signal, update, chat, event_key)`` is what stops the repeat."""
    events = [event("ENTRY_FILLED", payload={"rung": "1", "weight_pct": "40"})]
    first = await notifier(store, fake_bot, bot_config, events).deliver()
    second = await notifier(store, fake_bot, bot_config, events).deliver()

    assert (first, second) == (1, 0)
    assert len(fake_bot.of("send_message")) == 1


async def test_two_different_events_both_get_through(
    store: FakeStore, fake_bot: FakeBot, bot_config: AppConfig
) -> None:
    """The failure the old key had: with ``kind`` alone, only one update per signal
    could ever be claimed, so a stop-out after a fill would have been silent."""
    events = [
        event("ENTRY_FILLED", event_key="fill:0", payload={"rung": "1", "weight_pct": "40"}),
        event("STOPPED", event_key="stop", realized_r=Decimal("-0.40")),
    ]
    sent = await notifier(store, fake_bot, bot_config, events).deliver()

    assert sent == 2
    assert len(fake_bot.of("send_message")) == 2


async def test_a_dry_run_signal_says_nothing_at_all(
    store: FakeStore, fake_bot: FakeBot, bot_config: AppConfig
) -> None:
    """The whole point of the mode: the pipeline runs, the tracker resolves, and
    the phone stays silent. The event is still recorded and still measured."""
    store.signals[SIGNAL_ID].dry_run = True
    events = [event("STOPPED", realized_r=Decimal("-1.00"))]

    sent = await notifier(store, fake_bot, bot_config, events).deliver()

    assert sent == 0
    assert fake_bot.calls == [], "a rehearsal must make no outbound call whatsoever"


async def test_a_missing_card_still_gets_the_update_as_a_top_level_message(
    store: FakeStore, fake_bot: FakeBot, bot_config: AppConfig
) -> None:
    """A broken thread must not cost the owner the news that a position stopped."""
    del store.messages[(SIGNAL_ID, MessageKind.CARD.value, CHAT_ID, "")]
    events = [event("STOPPED", realized_r=Decimal("-1.00"))]

    sent = await notifier(store, fake_bot, bot_config, events).deliver()

    assert sent == 1
    assert fake_bot.of("send_message")[0].kwargs["reply_to_message_id"] is None


async def test_a_failed_send_is_recorded_and_does_not_raise(
    store: FakeStore, bot_config: AppConfig
) -> None:
    bot = FakeBot(fail={"send_message": RuntimeError("telegram is down")})
    events = [event("STOPPED", realized_r=Decimal("-1.00"))]

    sent = await notifier(store, bot, bot_config, events).deliver()

    assert sent == 0
    key = (SIGNAL_ID, MessageKind.UPDATE.value, CHAT_ID, "stopped")
    assert store.messages[key].status == "FAILED"


# --------------------------------------------------------------------------- #
# The decision acknowledgement, and the manage row
# --------------------------------------------------------------------------- #


def test_each_decision_says_what_it_actually_does() -> None:
    """The confirmation has to distinguish the three, because they mean three
    different things for the statistics — not just three different emoji."""
    taken = decision_ack_card(SignalDecision.TAKEN, 7, "SOLUSDT")
    assert "real stats" in taken and "open-risk budget" in taken

    watching = decision_ack_card(SignalDecision.WATCHING, 7, "SOLUSDT")
    assert "hypothetical" in watching and "No risk budget" in watching

    skipped = decision_ack_card(SignalDecision.SKIPPED, 7, "SOLUSDT")
    assert "still resolved in the background" in skipped


def test_the_manage_row_is_a_second_row_and_not_a_replacement() -> None:
    """A mis-tap must stay correctable after a fill (M6 decision 3) — which is
    exactly when the owner is tapping fastest."""
    keyboard = decision_keyboard_with_manage(SIGNAL_ID, SignalDecision.TAKEN)
    assert len(keyboard.inline_keyboard) == 2
    assert [button.text for button in keyboard.inline_keyboard[1]] == [
        "🔚 Closed manually",
        "✏️ Note",
    ]
    assert any(button.text == "» ✅ Taken «" for button in keyboard.inline_keyboard[0])


def test_manage_callbacks_fit_telegrams_payload_limit() -> None:
    """64 bytes, same check the decision buttons carry."""
    for button in manage_row(SIGNAL_ID):
        assert button.callback_data is not None
        assert len(button.callback_data.encode()) <= 64


def test_a_manage_callback_round_trips() -> None:
    packed = ManageCallback(signal_id=SIGNAL_ID, action=ManageAction.CLOSE).pack()
    unpacked = ManageCallback.unpack(packed)
    assert unpacked.signal_id == SIGNAL_ID
    assert unpacked.action is ManageAction.CLOSE
