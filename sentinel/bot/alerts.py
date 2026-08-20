"""Delivering admin alerts — ARCHITECTURE.md §2's "Telegram admin alert" (M8).

The mirror of ``notifier.py``, and the third instance of the same shape in this
codebase: a pure decision (``core/alerts.py``), a read model, a renderer, and this
— the only part that talks to Telegram. It exists because the decision is worth
testing over every streak length in a table, and none of those tests should need a
bot.

Two things it is careful about:

**It never raises.** This is called from the scheduler's ``scan`` job, immediately
after the cycle it reports on. An alerter that threw on a Telegram outage would
take down the job whose failures it exists to announce — the failure mode
reporting on itself.

**It has no memory, on purpose.** Whether to alert is derived from the ``cycles``
rows every time (a streak that is a multiple of the threshold) and whether to
mention spend is derived from the *transition* the cycle just made
(``spend_state_before`` → ``spend_state_after``). Nothing is stored, so nothing
can get stuck, be lost in a restart, or need a migration — the same property that
makes the spend guard itself self-clearing at 00:00 UTC.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sentinel.bot.cards import alert_card
from sentinel.bot.publisher import SupportsSending
from sentinel.bot.readmodels import alert_view, spend_view
from sentinel.bot.views import AlertView
from sentinel.core.alerts import Alert, AlertKind, CycleOutcome, cycle_alert
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import Settings
from sentinel.core.logging import get_logger
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.core.orchestrator import CycleResult
from sentinel.llm.spend import SpendState, spend_window
from sentinel.storage.db import Database
from sentinel.storage.repositories import CycleRepository, LLMCallRepository

log = get_logger(__name__)


class MarketScopedFactory[T](Protocol):
    """How the alerter builds a market-bound repository (M10a).

    A protocol rather than ``Callable[[Any], T]``: the market is keyword-only, and a
    bare callable type would accept a test double that ignored it — which would
    quietly evaluate a failure streak against the wrong market's cycles.
    """

    def __call__(self, session: Any, *, market: Market) -> T: ...


#: Which spend transitions are worth a message, and what to call each one. A
#: state that did not change is not news; WARN → LIMIT_REACHED is.
_SPEND_ALERTS = {
    SpendState.WARN: AlertKind.SPEND_WARN,
    SpendState.LIMIT_REACHED: AlertKind.SPEND_LIMIT,
}


class AdminAlerter:
    """Sends what the last cycle justifies — **to the owner only** (M8.1).

    Cycle failures, recovery, and the spend guard's warn/limit notices are operator
    business: they are about the health of the pipeline and about money spent on the
    owner's Anthropic key. A member has no lever to pull in response to any of them,
    and telling them the system is broken would be alarming without being actionable.
    The caller passes ``chat_ids=(owner_id,)``; there is nothing here that decides
    who the owner is.
    """

    def __init__(
        self,
        database: Database,
        bot: SupportsSending,
        *,
        chat_ids: tuple[int, ...],
        settings: Settings,
        tz: ZoneInfo,
        market: Market = LEGACY_MARKET,
        clock: Clock | None = None,
        cycles: MarketScopedFactory[CycleRepository] = CycleRepository,
        llm_calls: MarketScopedFactory[LLMCallRepository] = LLMCallRepository,
    ) -> None:
        self._database = database
        self._bot = bot
        self._chat_ids = chat_ids
        self._settings = settings
        self._tz = tz
        #: Which market's cycles this alerter watches (M10a). A failure streak is
        #: per market: with two markets running, "3 cycles failed" is a different
        #: and much less alarming statement when it is one market's scan and the
        #: other is healthy, and an alert that could not say which would send the
        #: owner to the wrong logs at 3am.
        self._market = market
        self._clock = clock or SystemClock()
        self._cycles = cycles
        self._llm_calls = llm_calls

    async def after_cycle(self, result: CycleResult) -> int:
        """Evaluate and deliver. Returns how many messages were sent."""
        sent = 0
        try:
            for view in await self._pending(result):
                sent += await self._send(view)
        except Exception as exc:  # pragma: no cover — defence in depth
            log.error("alert.evaluation_failed", error=str(exc), error_type=type(exc).__name__)
        return sent

    async def _pending(self, result: CycleResult) -> list[AlertView]:
        views: list[AlertView] = []
        failure = await self._cycle_alert()
        if failure is not None:
            views.append(alert_view(failure))
        spend = await self._spend_alert(result)
        if spend is not None:
            views.append(spend)
        return views

    async def _cycle_alert(self) -> Alert | None:
        config = self._settings.config
        threshold = config.alerts.consecutive_cycle_failures
        # This market's own interval, not the global default (M10a): the rail asks
        # "has a cycle been RUNNING for longer than it should", and "should" is a
        # per-market figure now. Reading the shared default would make a market on a
        # slower cadence look permanently stuck.
        stale_after = timedelta(
            minutes=config.market(self._market).scan_interval_minutes
            * config.alerts.stale_cycle_multiplier
        )
        async with self._database.session() as session:
            # One more row than the threshold needs, so a recovery has the run
            # behind it to look at.
            rows = await self._cycles(session, market=self._market).recent(limit=threshold * 4)
        return cycle_alert(
            [
                CycleOutcome(
                    status=row.status,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                    error=row.error,
                )
                for row in rows
            ],
            now=self._clock.now(),
            stale_after=stale_after,
            threshold=threshold,
        )

    async def _spend_alert(self, result: CycleResult) -> AlertView | None:
        """A message only when this cycle *crossed* a level.

        journal/M7_REPORT.md §1 and docs/MILESTONES.md both promised "a Telegram
        notice when either trips" and no code ever sent one — found while writing
        M8's verification step, closed here (journal/M8_REPORT.md §4).
        """
        before, after = result.spend_state_before, result.spend_state_after
        if after is None or before == after:
            return None
        kind = _SPEND_ALERTS.get(after)
        if kind is None:
            return None
        day_start, month_start = spend_window(self._clock.now())
        async with self._database.session() as session:
            totals = await self._llm_calls(session, market=self._market).spend_totals(
                day_start=day_start,
                month_start=month_start,
                priced_models=tuple(self._settings.config.llm.pricing),
            )
        return alert_view(
            Alert(kind=kind),
            spend=spend_view(totals, self._settings.config.llm),
        )

    async def _send(self, view: AlertView) -> int:
        text = alert_card(view, self._tz)
        sent = 0
        for chat_id in self._chat_ids:
            try:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=self._settings.config.telegram.parse_mode,
                    reply_markup=None,  # type: ignore[arg-type]
                    reply_to_message_id=None,
                )
            except Exception as exc:
                # Logged, never raised: see the module docstring. There is no
                # retry either — an alert is only worth reading while it is true,
                # and the next cycle re-derives whether it still is.
                log.error(
                    "alert.send_failed",
                    kind=view.kind,
                    chat_id=chat_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            sent += 1
            log.warning("alert.sent", kind=view.kind, chat_id=chat_id)
        return sent


__all__ = ["AdminAlerter"]
