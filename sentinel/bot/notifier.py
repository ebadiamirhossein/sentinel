"""Posting the tracker's events as replies under the original card (§4).

The mirror of ``publisher.py``, and deliberately a separate process step from the
tracker that produced the events. ``sentinel/tracker/loop.py`` records what the
market did; this drains what has not been said yet. Two reasons:

* **Module direction.** CLAUDE.md forbids a sideways import between pipeline
  stages, and the tracker importing the bot's renderer would be exactly that.
* **The crash window.** Because recording and posting are separate and each is
  idempotent on its own key, a process that dies between them resolves forward:
  the event is written and not yet posted, and the next tick posts it. There is no
  state in which an event is both lost and believed sent.

Idempotency is the same claim-commit-send-confirm as the card, keyed on
``(signal_id, 'update', chat_id, event_key)`` — the fourth column added in M7,
because a signal's thread carries a dozen updates and ``kind`` alone allowed one.

**Dry-run signals are skipped entirely.** A rehearsal cycle must produce a
completely silent phone; the events are still recorded and still measured.

**M8.1 — one recipient at a time, and the queue is theirs.** ``chat_ids`` is now the
list of users eligible to receive anything, and ``unposted`` is asked *per user* as
well as per chat. That second filter is not a refinement: "unposted" used to mean
"no message row for this chat", which for a single owner was the same thing as
"mine". With several users it is not, and without the filter every member would
receive fill-by-fill commentary on every other member's positions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sentinel.bot.cards import tracker_update_card
from sentinel.bot.models import MessageKind
from sentinel.bot.publisher import MessageStore, SupportsSending
from sentinel.bot.readmodels import tracker_event_view
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import TelegramConfig
from sentinel.core.logging import get_logger
from sentinel.storage.db import Database
from sentinel.storage.models import SignalEventRow
from sentinel.storage.repositories import (
    SignalEventRepository,
    SignalRepository,
    TelegramMessageRepository,
)

log = get_logger(__name__)


class TrackerNotifier:
    """Deliver each unposted tracker event once, per chat."""

    def __init__(
        self,
        database: Database,
        bot: SupportsSending,
        *,
        chat_ids: tuple[int, ...],
        telegram: TelegramConfig,
        clock: Clock | None = None,
        messages: Callable[[Any], MessageStore] = TelegramMessageRepository,
        events: Callable[[Any], SignalEventRepository] = SignalEventRepository,
        signals: Callable[[Any], SignalRepository] = SignalRepository,
    ) -> None:
        self._database = database
        self._bot = bot
        self._chat_ids = chat_ids
        self._telegram = telegram
        self._clock = clock or SystemClock()
        self._messages = messages
        self._events = events
        self._signals = signals

    async def deliver(self) -> int:
        """Post everything outstanding. Returns how many messages were sent.

        A private chat id equals its user id on Telegram, which is why one loop
        variable serves as both here — and why ``unposted`` is given it twice, once
        as the delivery key and once as the ownership filter. They are the same
        number and two different questions.
        """
        sent = 0
        for chat_id in self._chat_ids:
            async with self._database.session() as session:
                pending = await self._events(session).unposted(chat_id, user_id=chat_id)
            for event in pending:
                sent += await self._deliver_one(event, chat_id)
        return sent

    async def _deliver_one(self, event: SignalEventRow, chat_id: int) -> int:
        async with self._database.session() as session:
            row = await self._signals(session).get(event.signal_id)
        if row is None:  # pragma: no cover — an event always has its signal
            return 0
        if row.dry_run:
            # Recorded and measured, never spoken. The whole point of the mode.
            return 0

        if not await self._claim(event, chat_id):
            return 0

        text = tracker_update_card(tracker_event_view(event, row))
        try:
            sent = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=self._telegram.parse_mode,
                reply_markup=None,  # type: ignore[arg-type]
                reply_to_message_id=await self._card_message_id(event, chat_id),
            )
        except Exception as exc:
            await self._fail(event, chat_id, exc)
            return 0

        await self._confirm(event, chat_id, int(sent.message_id))
        log.info(
            "bot.tracker_update_posted",
            signal_id=str(event.signal_id),
            number=row.number,
            symbol=row.symbol,
            event_key=event.event_key,
            chat_id=chat_id,
        )
        return 1

    async def _card_message_id(self, event: SignalEventRow, chat_id: int) -> int | None:
        """§4's updates are *replies*, so they need the card's message id.

        A missing card — the signal was never posted, or its claim is stuck — posts
        the update as a top-level message rather than dropping it. The owner should
        hear that a position stopped out even if the thread is broken.
        """
        async with self._database.session() as session:
            posted = await self._messages(session).get(event.signal_id, MessageKind.CARD, chat_id)
        return None if posted is None else posted.message_id

    async def _claim(self, event: SignalEventRow, chat_id: int) -> bool:
        async with self._database.session() as session:
            claimed = await self._messages(session).claim(
                event.signal_id,
                MessageKind.UPDATE,
                chat_id,
                at=self._clock.now(),
                event_key=event.event_key,
            )
            await session.commit()
        return claimed

    async def _confirm(self, event: SignalEventRow, chat_id: int, message_id: int) -> None:
        async with self._database.session() as session:
            await self._messages(session).confirm(
                event.signal_id,
                MessageKind.UPDATE,
                chat_id,
                message_id=message_id,
                at=self._clock.now(),
                event_key=event.event_key,
            )
            await session.commit()

    async def _fail(self, event: SignalEventRow, chat_id: int, exc: Exception) -> None:
        log.error(
            "bot.tracker_update_failed",
            signal_id=str(event.signal_id),
            event_key=event.event_key,
            chat_id=chat_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        async with self._database.session() as session:
            await self._messages(session).fail(
                event.signal_id,
                MessageKind.UPDATE,
                chat_id,
                error=str(exc),
                event_key=event.event_key,
            )
            await session.commit()


__all__ = ["TrackerNotifier"]
