"""Chat-level notices that belong to a *user* rather than to a signal (M8.1).

Two of them exist, and both answer the same question from the other side: **why did
nothing arrive?**

* *Your capital is not set* — a plan was approved and could not be sized.
* *Daily loss limit reached* — specs/TELEGRAM_UX.md §4's notice, which the tracker
  has been raising since M7 with nothing to deliver it.

Silence is the failure mode this milestone is most exposed to. A member whose
signals stop has no ``/status`` to consult (that is owner-only) and no way to tell
"the market is quiet" from "the system has stopped talking to me". Every reason a
delivery is withheld therefore has a sentence attached to it.

**Idempotency without a signal to hang off.** ``telegram_messages.signal_id`` is
``NOT NULL``, and these notices have no signal. They claim a deterministic
``uuid5`` of their own key instead — M7's pattern for the pause and spend notices,
reused rather than reinvented — so the key carries the user and the UTC date, each
notice is sent at most once per user per day, and a restart never repeats one. No
column was made nullable to achieve it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sentinel.bot.models import MessageKind
from sentinel.bot.publisher import MessageStore, SupportsSending
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import TelegramConfig
from sentinel.core.logging import get_logger
from sentinel.storage.db import Database
from sentinel.storage.repositories import TelegramMessageRepository

log = get_logger(__name__)


def notice_id(key: str) -> UUID:
    """A stable stand-in ``signal_id`` for a notice that has no signal."""
    return uuid5(NAMESPACE_URL, f"sentinel-notice/{key}")


def no_capital_key(user_id: int, at: datetime) -> str:
    """One per user per UTC day. Nagging hourly would train them to ignore it."""
    return f"no-capital:{user_id}:{at:%Y-%m-%d}"


def loss_pause_key(user_id: int, at: datetime) -> str:
    return f"loss-pause:{user_id}:{at:%Y-%m-%d}"


def calendar_coverage_key(at: datetime) -> str:
    """One per UTC day, and deliberately not per user (M10d, specs/FOREX.md §8).

    The economic calendar is an operator concern: a member has no lever to pull and
    cannot edit a YAML file on the server. It goes to the owner alone, exactly as the
    admin alerts do.

    Daily rather than per cycle, because the condition holds for up to a fortnight
    before coverage lapses and a message every hour for two weeks is a message that
    gets muted — which would turn the rail into the silence it exists to prevent.
    """
    return f"calendar-coverage:{at:%Y-%m-%d}"


def barren_market_key(market: str, at: datetime) -> str:
    """One per market per UTC day (M10e step 4, specs/FOREX.md §31).

    Per market, because a barren forex day says nothing about crypto and an alert that
    could not tell them apart would send the owner to the wrong logs. Per UTC day,
    because the condition it reports lasts a whole day: forex skipped every pair for
    thirteen consecutive cycles on 2026-08-24, and thirteen identical messages would
    have been muted long before anybody read one.

    Owner-only, like :func:`calendar_coverage_key` and for the same reason: a member has
    no lever to pull when a market stops ingesting.
    """
    return f"barren-market:{market}:{at:%Y-%m-%d}"


class UserNotifier:
    """Send one notice to one user, at most once per key.

    Mirrors ``SignalPublisher`` and ``TrackerNotifier``: claim, commit, send,
    confirm. A crash between the claim and the confirmation leaves a ``PENDING``
    row that is never retried, exactly as specs/TELEGRAM_UX.md §6 rules for the card
    — a duplicated notice is noise, and ``/status`` counts the stuck row.
    """

    def __init__(
        self,
        database: Database,
        bot: SupportsSending,
        *,
        telegram: TelegramConfig,
        clock: Clock | None = None,
        messages: Callable[[Any], MessageStore] = TelegramMessageRepository,
    ) -> None:
        self._database = database
        self._bot = bot
        self._telegram = telegram
        self._clock = clock or SystemClock()
        self._messages = messages

    async def notice(self, user_id: int, *, key: str, text: str) -> bool:
        """``True`` if this call is the one that delivered it."""
        signal_id = notice_id(key)
        now = self._clock.now()

        async with self._database.session() as session:
            claimed = await self._messages(session).claim(
                signal_id, MessageKind.UPDATE, user_id, at=now, event_key=key
            )
            await session.commit()
        if not claimed:
            return False

        try:
            sent = await self._bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=self._telegram.parse_mode,
                reply_markup=None,  # type: ignore[arg-type]
                reply_to_message_id=None,
            )
        except Exception as exc:
            async with self._database.session() as session:
                await self._messages(session).fail(
                    signal_id, MessageKind.UPDATE, user_id, error=str(exc), event_key=key
                )
                await session.commit()
            log.warning("bot.notice_failed", user_id=user_id, key=key, error=str(exc))
            return False

        async with self._database.session() as session:
            await self._messages(session).confirm(
                signal_id,
                MessageKind.UPDATE,
                user_id,
                message_id=int(sent.message_id),
                at=now,
                event_key=key,
            )
            await session.commit()
        log.info("bot.notice_sent", user_id=user_id, key=key)
        return True


__all__ = [
    "UserNotifier",
    "barren_market_key",
    "calendar_coverage_key",
    "loss_pause_key",
    "no_capital_key",
    "notice_id",
]
