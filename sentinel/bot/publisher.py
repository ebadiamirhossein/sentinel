"""Posting a signal, exactly once (specs/TELEGRAM_UX.md §1 and §6).

This is the seam M7's cycle orchestrator calls: a gate-approved ``TradePlan`` and
its charts in, a delivered card out, and nothing delivered twice.

**Why the unit of work is split.** Repositories in this codebase never commit —
the caller owns the transaction. A publisher cannot follow that rule, because the
ordering *is* the guarantee:

1. claim the signal and its message rows, then **commit** — the claim is durable
   before anything leaves the process;
2. send to Telegram;
3. record the returned message ids, and commit again.

Claiming after the send would lose the id if the process died in between, and a
single transaction around all three would roll the claim back on a crash and
re-post the card on the next start. A crash between 1 and 3 therefore leaves a
``PENDING`` claim, which is deliberately **never retried**: a duplicated signal
card is worse than a missing one the owner can ask for again, and ``/status``
reports the stuck row so it is never silent.

The album goes first and the card replies to it. A Telegram media group cannot
carry an inline keyboard, and its caption caps at 1024 characters against a card
of roughly 1,600 — so "charts attached as an album above the card" has to be two
messages, not a captioned one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InputMediaPhoto

from sentinel.bot.cards import signal_card
from sentinel.bot.keyboards import decision_keyboard
from sentinel.bot.models import MessageKind, SignalRecord
from sentinel.charts.models import ChartImage
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import TelegramConfig
from sentinel.core.logging import get_logger
from sentinel.risk.models import TradePlan
from sentinel.storage.db import Database
from sentinel.storage.repositories import SignalRepository, TelegramMessageRepository

log = get_logger(__name__)


class SignalStore(Protocol):
    """The publisher's half of ``SignalRepository``."""

    async def claim(self, record: SignalRecord) -> SignalRecord | None: ...


class MessageStore(Protocol):
    """The publisher's half of ``TelegramMessageRepository``.

    Narrow protocols, and repository *factories* on the constructor, so the
    hermetic suite can exercise the claim-then-send ordering without a live
    Postgres. The idempotency guarantee is the headline requirement of this
    milestone (specs/TELEGRAM_UX.md §6); testing it only when a developer happens
    to have set ``SENTINEL_TEST_DATABASE_URL`` would leave it effectively untested.
    ``tests/bot/test_persistence.py`` re-runs the same scenarios against real
    Postgres, so the constraint itself is proven too.
    """

    async def claim(
        self, signal_id: UUID, kind: MessageKind, chat_id: int, *, at: datetime
    ) -> bool: ...

    async def confirm(
        self,
        signal_id: UUID,
        kind: MessageKind,
        chat_id: int,
        *,
        message_id: int,
        at: datetime,
    ) -> None: ...

    async def fail(
        self, signal_id: UUID, kind: MessageKind, chat_id: int, *, error: str
    ) -> None: ...


class SupportsSending(Protocol):
    """What the publisher needs from a bot — narrow, so tests can supply a double.

    Mirrors ``storage.db.SupportsPing``: the seam is the few methods actually used,
    not the whole aiogram surface. The signatures name the exact keywords the
    publisher passes, so ``mypy --strict`` checks that a real ``aiogram.Bot``
    satisfies it — a ``**kwargs: Any`` protocol would accept anything and prove
    nothing.
    """

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str,
        reply_markup: InlineKeyboardMarkup,
        reply_to_message_id: int | None,
    ) -> Any: ...

    # ``list`` is invariant, and aiogram accepts a union of five media types, so
    # this stays ``list[Any]``: naming ``list[InputMediaPhoto]`` would make a real
    # Bot fail the protocol. The elements are constructed as InputMediaPhoto below
    # and typed there.
    async def send_media_group(self, *, chat_id: int, media: list[Any]) -> Sequence[Any]: ...


class PublishResult:
    """What happened, in enough detail for a caller to log or assert on it."""

    def __init__(self, record: SignalRecord | None, *, published: bool, reason: str = "") -> None:
        self.record = record
        self.published = published
        self.reason = reason


class SignalPublisher:
    """Deliver one approved plan to the owner's chats, idempotently."""

    def __init__(
        self,
        database: Database,
        bot: SupportsSending,
        *,
        chat_ids: tuple[int, ...],
        telegram: TelegramConfig,
        tz: Any,
        clock: Clock | None = None,
        signals: Callable[[Any], SignalStore] = SignalRepository,
        messages: Callable[[Any], MessageStore] = TelegramMessageRepository,
    ) -> None:
        self._database = database
        self._bot = bot
        self._chat_ids = chat_ids
        self._telegram = telegram
        self._tz = tz
        self._clock = clock or SystemClock()
        self._signals = signals
        self._messages = messages

    async def publish(
        self,
        plan: TradePlan,
        charts: tuple[ChartImage, ...] = (),
        *,
        cycle_id: UUID | None = None,
    ) -> PublishResult:
        record = SignalRecord(
            plan=plan,
            cycle_id=cycle_id,
            chart_params=tuple(chart.params.to_json_dict() for chart in charts),
        )

        async with self._database.session() as session:
            claimed = await self._signals(session).claim(record)
            if claimed is None:
                await session.rollback()
                log.info("bot.signal_already_published", plan_id=str(plan.plan_id))
                return PublishResult(None, published=False, reason="plan already published")
            await session.commit()
        record = claimed

        posted_any = False
        for chat_id in self._chat_ids:
            posted_any = await self._publish_to_chat(record, charts, chat_id) or posted_any

        return PublishResult(record, published=posted_any)

    async def _publish_to_chat(
        self, record: SignalRecord, charts: tuple[ChartImage, ...], chat_id: int
    ) -> bool:
        album_message_id = await self._send_album(record, charts, chat_id)
        return await self._send_card(record, chat_id, reply_to=album_message_id)

    async def _send_album(
        self, record: SignalRecord, charts: tuple[ChartImage, ...], chat_id: int
    ) -> int | None:
        selected = self._selected_charts(charts)
        if not selected:
            return None
        if not await self._claim(record.signal_id, MessageKind.CHARTS, chat_id):
            return None

        media = [
            InputMediaPhoto(
                media=BufferedInputFile(
                    chart.png,
                    filename=f"{chart.params.spec.symbol}_{chart.params.spec.timeframe}.png",
                )
            )
            for chart in selected
        ]
        try:
            sent = await self._bot.send_media_group(chat_id=chat_id, media=media)
        except Exception as exc:
            await self._fail(record.signal_id, MessageKind.CHARTS, chat_id, exc)
            return None

        message_id = int(sent[0].message_id)
        await self._confirm(record.signal_id, MessageKind.CHARTS, chat_id, message_id)
        return message_id

    async def _send_card(self, record: SignalRecord, chat_id: int, *, reply_to: int | None) -> bool:
        if not await self._claim(record.signal_id, MessageKind.CARD, chat_id):
            log.info(
                "bot.card_already_claimed",
                signal_id=str(record.signal_id),
                chat_id=chat_id,
                detail="not re-sent — see specs/TELEGRAM_UX.md §6",
            )
            return False

        keyboard: InlineKeyboardMarkup = decision_keyboard(record.signal_id, record.decision)
        try:
            sent = await self._bot.send_message(
                chat_id=chat_id,
                text=signal_card(record, self._tz),
                parse_mode=self._telegram.parse_mode,
                reply_markup=keyboard,
                reply_to_message_id=reply_to,
            )
        except Exception as exc:
            await self._fail(record.signal_id, MessageKind.CARD, chat_id, exc)
            return False

        await self._confirm(record.signal_id, MessageKind.CARD, chat_id, int(sent.message_id))
        log.info(
            "bot.card_posted",
            signal_id=str(record.signal_id),
            number=record.number,
            symbol=record.plan.symbol,
            chat_id=chat_id,
        )
        return True

    def _selected_charts(self, charts: tuple[ChartImage, ...]) -> list[ChartImage]:
        """§1 attaches the 1h and 4h charts; the 15m is a timing detail the entry
        zone already encodes. Configurable, because that is a display choice."""
        wanted = self._telegram.card_chart_timeframes
        chosen = [chart for chart in charts if chart.params.spec.timeframe in wanted]
        return sorted(chosen, key=lambda chart: wanted.index(chart.params.spec.timeframe))

    async def _claim(self, signal_id: UUID, kind: MessageKind, chat_id: int) -> bool:
        async with self._database.session() as session:
            claimed = await self._messages(session).claim(
                signal_id, kind, chat_id, at=self._clock.now()
            )
            await session.commit()
        return claimed

    async def _confirm(
        self, signal_id: UUID, kind: MessageKind, chat_id: int, message_id: int
    ) -> None:
        async with self._database.session() as session:
            await self._messages(session).confirm(
                signal_id, kind, chat_id, message_id=message_id, at=self._clock.now()
            )
            await session.commit()

    async def _fail(self, signal_id: UUID, kind: MessageKind, chat_id: int, exc: Exception) -> None:
        log.error(
            "bot.send_failed",
            signal_id=str(signal_id),
            kind=kind.value,
            chat_id=chat_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        async with self._database.session() as session:
            await self._messages(session).fail(signal_id, kind, chat_id, error=str(exc))
            await session.commit()


__all__ = ["MessageStore", "PublishResult", "SignalPublisher", "SignalStore", "SupportsSending"]
