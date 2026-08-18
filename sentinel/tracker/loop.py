"""The 60-second tick: observe, advance, persist. Nothing else.

ARCHITECTURE.md §3's tracker loop, and §6's "Process crash -> ... tracker rebuilds
state from DB". There is no recovery routine here because there is nothing to
recover: **the loop holds no state between ticks.** Every tick asks the database
which signals are open, rebuilds each one's history from ``signal_fills`` and
``signal_exits``, and re-derives what the market did from candles that are still
there. A restart is simply the next tick.

What makes that safe rather than merely stateless is the pair of unique
constraints. A fill is unique on ``(signal_id, rung_index)``, an event on
``(signal_id, event_key)``. So a tick interrupted halfway replays the same
detection and writes nothing twice — and the state machine refuses the replayed
events with a reason rather than treating them as new.

**Isolation.** One signal's failure must never stop the others, exactly as PRD F1
requires of a scan cycle: a symbol whose candles will not load is skipped and
logged, and the rest of the book is still managed.

**No Telegram here.** The loop records events; ``sentinel/bot/notifier.py`` posts
them. Keeping the two apart is CLAUDE.md's module direction (never a sideways
import between stages), and it is also why a crash between recording and posting
resolves forward instead of losing one or duplicating the other.

**No LLM here, ever** — and that is load-bearing: the spend guard suspends deep
analysis on a bad day, and it is only safe to let it do that because this loop
manages open positions without one. ``tests/llm/test_spend.py`` asserts the import
graph, not just the behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sentinel.bot.models import TERMINAL_STATUSES, SignalStatus
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import Settings
from sentinel.core.logging import get_logger
from sentinel.risk.accounting import avg_fill_price, realized_r
from sentinel.risk.costs import funding_settlements, realized_costs_eur
from sentinel.risk.models import PauseReason, PauseState, TradePlan
from sentinel.risk.rails import evaluate_daily_loss, realized_loss_pct
from sentinel.risk.rounding import money, ratio
from sentinel.storage.db import Database
from sentinel.storage.models import SignalRow
from sentinel.storage.repositories import (
    RiskStateRepository,
    RuntimeSettingsRepository,
    SignalEventRepository,
    SignalExitRepository,
    SignalFillRepository,
    SignalRepository,
)
from sentinel.tracker.detect import observe
from sentinel.tracker.machine import advance
from sentinel.tracker.models import (
    EXIT_MANUAL,
    EXIT_STOP,
    LegExit,
    RungFill,
    SignalTracking,
    TrackerEvent,
)
from sentinel.tracker.prices import PriceFeed

log = get_logger(__name__)

#: Which exit ends a signal, as ``signals.outcome``.
OUTCOME_FOR_STATUS = {
    SignalStatus.STOPPED: "STOP",
    SignalStatus.INVALIDATED: "INVALIDATION",
    SignalStatus.EXPIRED: "EXPIRY",
}


@dataclass(frozen=True)
class TrackerRepositories:
    """Which repository classes a tick instantiates per session.

    The same seam ``bot/context.py`` carries, for the same reason: crash recovery
    and event idempotency are this milestone's headline guarantees, and testing
    them only when a developer happens to have ``SENTINEL_TEST_DATABASE_URL`` set
    would leave them effectively untested. The fakes subclass the real
    repositories, so a drifting signature fails ``mypy --strict``.
    """

    signals: type[SignalRepository] = SignalRepository
    fills: type[SignalFillRepository] = SignalFillRepository
    exits: type[SignalExitRepository] = SignalExitRepository
    events: type[SignalEventRepository] = SignalEventRepository
    risk_state: type[RiskStateRepository] = RiskStateRepository
    settings: type[RuntimeSettingsRepository] = RuntimeSettingsRepository


@dataclass
class TickResult:
    """What one tick did — logged, and asserted on in tests."""

    checked: int = 0
    events: int = 0
    resolved: int = 0
    skipped: int = 0
    paused: bool = False
    failures: list[str] = field(default_factory=list)


def already_paused_for_loss(current: PauseState, now: datetime) -> bool:
    """Is a loss-limit pause already running?

    The tick recomputes the day's realized loss every 60 seconds, so a bad day
    keeps producing the same verdict. Without this, each tick would re-save the
    pause with a fresh ``now + 24h`` — sliding the window forward forever, so the
    pause would never lapse and the §4 notice would be posted every minute.
    """
    return (
        current.paused and current.reason is PauseReason.DAILY_LOSS_LIMIT and current.is_active(now)
    )


class TrackerLoop:
    """Runs one tick over every open signal."""

    def __init__(
        self,
        database: Database,
        feed: PriceFeed,
        settings: Settings,
        *,
        clock: Clock | None = None,
        repositories: TrackerRepositories | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._settings = settings
        self._clock = clock or SystemClock()
        self._repos = repositories or TrackerRepositories()

    async def tick(self) -> TickResult:
        now = self._clock.now()
        result = TickResult()

        async with self._database.session() as session:
            rows = await self._repos.signals(session).open_signals()

        for row in rows:
            try:
                await self._advance_signal(row, now=now, result=result)
            except Exception as exc:
                # PRD F1's rule, applied per signal: one symbol's problem never
                # stops the others. A signal that could not be checked is left
                # exactly as it was, and the next tick tries again.
                result.skipped += 1
                result.failures.append(f"{row.symbol}: {exc}")
                log.warning(
                    "tracker.signal_failed",
                    signal_id=str(row.id),
                    symbol=row.symbol,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        result.paused = await self._enforce_daily_loss(now=now)
        log.info(
            "tracker.tick",
            checked=result.checked,
            events=result.events,
            resolved=result.resolved,
            skipped=result.skipped,
            paused=result.paused,
        )
        return result

    # ---- one signal --------------------------------------------------------

    async def _advance_signal(self, row: SignalRow, *, now: datetime, result: TickResult) -> None:
        plan = TradePlan.model_validate(row.plan)
        # Captured before anything advances: ``SignalRepository.advance`` mutates
        # the ORM row in place, so a comparison made afterwards would always find
        # the status it had just written.
        was = SignalStatus(row.status)

        async with self._database.session() as session:
            fills = await self._repos.fills(session).for_signal(row.id)
            exits = await self._repos.exits(session).for_signal(row.id)

        tracking = SignalTracking(
            plan=plan,
            status=SignalStatus(row.status),
            fills=tuple(
                RungFill(
                    rung_index=fill.rung_index, price=fill.price, qty=fill.qty, at=fill.filled_at
                )
                for fill in fills
            ),
            exits=tuple(
                LegExit(kind=exit_.kind, price=exit_.price, qty=exit_.qty, at=exit_.exited_at)
                for exit_ in exits
            ),
            stop_price=row.stop_price_current,
        )

        # The window runs from the last tick, or — the first time this signal is
        # seen — from when it was published. Anything less would let a signal
        # created before the tracker last ran resolve on its aftermath instead of
        # on what actually happened to it.
        candles = await self._feed.fill_candles(
            row.symbol, since=row.last_checked_at or row.created_at, now=now
        )
        closed_1h = await self._feed.invalidation_candles(row.symbol) if not tracking.fills else ()
        events = observe(tracking, candles=candles, closed_1h=closed_1h, now=now)
        result.checked += 1

        journal: list[TrackerEvent] = []
        for event in events:
            transition = advance(tracking, event, management=self._settings.config.management)
            if not transition.applied:
                log.debug(
                    "tracker.event_refused",
                    signal_id=str(row.id),
                    kind=event.kind.value,
                    reason=transition.reason,
                )
                continue

            tracking = tracking.model_copy(
                update={
                    "status": transition.status,
                    "fills": (
                        (*tracking.fills, transition.fill)
                        if transition.fill is not None
                        else tracking.fills
                    ),
                    "exits": (
                        (*tracking.exits, transition.exit)
                        if transition.exit is not None
                        else tracking.exits
                    ),
                    "stop_price": (
                        transition.stop_price
                        if transition.stop_price is not None
                        else tracking.stop_price
                    ),
                }
            )
            if transition.event is not None:
                journal.append(transition.event)
            journal.extend(transition.extra_events)

        await self._persist(row, tracking, journal, now=now)
        result.events += len(journal)
        if tracking.status in TERMINAL_STATUSES and was not in TERMINAL_STATUSES:
            result.resolved += 1

    async def _persist(
        self,
        row: SignalRow,
        tracking: SignalTracking,
        journal: list[TrackerEvent],
        *,
        now: datetime,
    ) -> None:
        """One transaction per signal: legs, journal, roll-ups.

        Per signal rather than per tick, so a failure while writing one signal
        cannot roll back another's fills. The unique constraints make a repeat of
        any of it a no-op.
        """
        async with self._database.session() as session:
            fills_repo = self._repos.fills(session)
            exits_repo = self._repos.exits(session)
            events_repo = self._repos.events(session)

            for fill in tracking.fills:
                await fills_repo.record(
                    row.id,
                    rung_index=fill.rung_index,
                    price=fill.price,
                    qty=fill.qty,
                    filled_at=fill.at,
                    detected_at=now,
                )
            for exit_ in tracking.exits:
                await exits_repo.record(
                    row.id,
                    kind=exit_.kind,
                    price=exit_.price,
                    qty=exit_.qty,
                    exited_at=exit_.at,
                    detected_at=now,
                )
            for event in journal:
                await events_repo.record(
                    row.id,
                    event_key=event.event_key,
                    kind=event.kind.value,
                    at=event.at,
                    from_status=None if event.from_status is None else event.from_status.value,
                    to_status=None if event.to_status is None else event.to_status.value,
                    price=event.price,
                    realized_r=event.realized_r,
                    realized_eur=event.realized_eur,
                    payload=dict(event.payload),
                    detail=event.detail,
                )

            await self._repos.signals(session).advance(row.id, **self._rollup(tracking, now=now))
            await session.commit()

        for event in journal:
            log.info(
                "tracker.event",
                signal_id=str(row.id),
                number=row.number,
                symbol=row.symbol,
                kind=event.kind.value,
                event_key=event.event_key,
                to_status=None if event.to_status is None else event.to_status.value,
                realized_r=None if event.realized_r is None else str(event.realized_r),
                dry_run=row.dry_run,
            )

    def _rollup(self, tracking: SignalTracking, *, now: datetime) -> dict[str, object]:
        """The derived columns on ``signals``, recomputed from the legs.

        Derived and not authoritative: ``signal_fills`` and ``signal_exits`` are the
        record, and these exist so /positions, /stats and the rails read one row
        instead of three tables.
        """
        plan = tracking.plan
        terminal = tracking.status in TERMINAL_STATUSES
        fills = tuple(fill.as_fill() for fill in tracking.fills)
        exits = tuple(exit_.as_exit() for exit_ in tracking.exits if exit_.qty > 0)

        fields: dict[str, object] = {
            "status": tracking.status.value,
            "filled_qty": tracking.filled_qty,
            "avg_fill_price": avg_fill_price(fills) if fills else None,
            "stop_price_current": tracking.stop_price,
            "tp_hits": len(tracking.hit_targets),
            "last_checked_at": now,
        }
        if tracking.fills:
            fields["first_fill_at"] = min(fill.at for fill in tracking.fills)

        if exits:
            r = realized_r(
                direction=plan.direction,
                fills=fills,
                exits=exits,
                planned_risk_usdt=plan.planned_risk_eur * plan.eurusd_rate,
            )
            fields["realized_r"] = ratio(r)
            fields["realized_eur"] = money(r * plan.planned_risk_eur)
            fields["realized_costs_eur"] = self._realized_costs(tracking, now=now)

        if terminal:
            fields["closed_at"] = max((exit_.at for exit_ in tracking.exits), default=now)
            fields["outcome"] = self._outcome(tracking)
        return fields

    def _realized_costs(self, tracking: SignalTracking, *, now: datetime) -> Decimal:
        """M5.1 §10: charge what the trade cost, not what the plan estimated."""
        plan = tracking.plan
        opened = min((fill.at for fill in tracking.fills), default=plan.created_at)
        closed = max((exit_.at for exit_ in tracking.exits), default=now)
        settlements = funding_settlements(
            created_at=opened,
            expires_at=closed,
            # The plan does not carry the exchange's published next settlement, so
            # the window is divided by the interval — the honest fallback §4.2
            # already specifies for that case.
            next_funding_time=None,
            interval_hours=plan.costs.funding_interval_hours,
        )
        costs = realized_costs_eur(
            direction=plan.direction,
            fills=tuple(fill.as_fill() for fill in tracking.fills),
            exits=tuple(exit_.as_exit() for exit_ in tracking.exits if exit_.qty > 0),
            eurusd_rate=plan.eurusd_rate,
            funding_rate=plan.costs.funding_rate,
            settlements=settlements,
            config=self._settings.config.costs,
        )
        return costs.total_eur

    @staticmethod
    def _outcome(tracking: SignalTracking) -> str:
        named = OUTCOME_FOR_STATUS.get(tracking.status)
        if named is not None:
            return named
        # CLOSED: whichever leg finished it. A manual close is named as one, so
        # M9 can separate the owner's judgement from the plan running its course.
        for exit_ in reversed(tracking.exits):
            if exit_.kind in {EXIT_MANUAL, EXIT_STOP} or exit_.kind.startswith("TP"):
                return exit_.kind
        return "CLOSED"  # pragma: no cover — CLOSED always follows an exit

    # ---- the rail that runs on every tick (§7) -----------------------------

    async def _enforce_daily_loss(self, *, now: datetime) -> bool:
        """§7: "checked before every signal AND on every tracker tick".

        The window is the UTC calendar day (owner ruling, M7), so it resets at the
        same instant every stored row is stamped against. A quiet day never lifts
        an existing pause — only ``/resume`` does, and a loss-limit pause needs the
        confirmation button (§3).
        """
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._database.session() as session:
            realized = await self._repos.signals(session).realized_eur_since(day_start)
            stored = await self._repos.settings(session).all()
            current = await self._repos.risk_state(session).load()

        capital = stored.get("capital_eur")
        loss_pct = realized_loss_pct(
            realized, capital_eur=None if capital is None else Decimal(str(capital))
        )
        state = evaluate_daily_loss(
            realized_loss_pct=loss_pct,
            limit_pct=self._settings.config.risk.daily_loss_limit_pct,
            now=now,
            current=current,
        )
        newly_paused = (
            state.paused
            and state.reason is PauseReason.DAILY_LOSS_LIMIT
            and not already_paused_for_loss(current, now)
        )
        if newly_paused:
            async with self._database.session() as session:
                await self._repos.risk_state(session).save(state)
                await session.commit()
            log.warning(
                "tracker.daily_loss_limit",
                realized_loss_pct=str(loss_pct),
                limit_pct=str(self._settings.config.risk.daily_loss_limit_pct),
                until=None if state.until is None else state.until.isoformat(),
            )
        return newly_paused


__all__ = [
    "OUTCOME_FOR_STATUS",
    "TickResult",
    "TrackerLoop",
    "TrackerRepositories",
    "already_paused_for_loss",
]
