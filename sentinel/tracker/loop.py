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

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sentinel.bot.models import TERMINAL_STATUSES, SignalStatus, UserAccount
from sentinel.bot.plans import eur_quote_rate_of, plan_of
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import Settings
from sentinel.core.logging import get_logger
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.fx.accounting import realized_costs_eur as forex_realized_costs_eur
from sentinel.fx.costs import rollover_nights
from sentinel.fx.plan import ForexPlan
from sentinel.risk.accounting import avg_fill_price, realized_r
from sentinel.risk.costs import funding_settlements, realized_costs_eur
from sentinel.risk.models import PauseReason, PauseState
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
    UserMarketPauseRepository,
    UserRepository,
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
    users: type[UserRepository] = UserRepository
    #: M10a — the per-market half of the daily-loss rail. ``users`` above keeps the
    #: combined-across-markets one, which is why both are here.
    user_market_pause: type[UserMarketPauseRepository] = UserMarketPauseRepository


@dataclass
class TickResult:
    """What one tick did — logged, and asserted on in tests."""

    checked: int = 0
    events: int = 0
    resolved: int = 0
    skipped: int = 0
    #: Users whose own daily-loss pause was raised by this tick (M8.1). A list
    #: rather than a bool because the rail is per user now, and because the caller
    #: has to tell each of them: a member has no ``/status`` and would otherwise
    #: just stop hearing from the system.
    paused_users: list[int] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def paused(self) -> bool:
        """Whether this tick paused anybody. Kept for the log line and the tools."""
        return bool(self.paused_users)


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
    """Runs one tick over every open signal **in one market**.

    The market is explicit rather than implied (M10a), and that is the whole reason
    it is a parameter: a ``PriceFeed`` wraps exactly one exchange adapter, so a
    tracker can only follow signals it can price. Leaving it to a default would mean
    that the day a second market has open signals, they are silently never tracked —
    fills missed, stops missed, outcomes never resolved — and nothing would say so.
    That is the silence-as-success failure this project has met twice.

    M10b adds a second feed and a second loop; until then there is one adapter and
    one market, and this states which.
    """

    def __init__(
        self,
        database: Database,
        feed: PriceFeed,
        settings: Settings,
        *,
        market: Market = LEGACY_MARKET,
        clock: Clock | None = None,
        repositories: TrackerRepositories | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._settings = settings
        #: Which market this loop can price, and therefore the only one whose
        #: signals it reads. Every repository below is bound to it.
        self._market = market
        self._clock = clock or SystemClock()
        self._repos = repositories or TrackerRepositories()

    async def tick(self) -> TickResult:
        now = self._clock.now()
        result = TickResult()
        # One tick, one set of candles. Several users' signals on the same symbol are
        # asking the exchange the identical question (M8.1).
        self._feed.reset()

        async with self._database.session() as session:
            rows = await self._repos.signals(session, market=self._market).open_signals()

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

        result.paused_users = await self._enforce_daily_loss(now=now)
        log.info(
            "tracker.tick",
            checked=result.checked,
            events=result.events,
            resolved=result.resolved,
            skipped=result.skipped,
            paused_users=result.paused_users,
        )
        return result

    # ---- one signal --------------------------------------------------------

    async def _advance_signal(self, row: SignalRow, *, now: datetime, result: TickResult) -> None:
        # Dispatched on the row's market, never guessed and never tried-then-fallen-
        # back (FOREX.md §16.7). ForexPlan mirrors TradePlan's vocabulary, so a
        # fallback would turn a mis-stamped row into a plausible plan rather than
        # into an error — and every R figure below would then be computed against it.
        plan = plan_of(row.plan, self._market)
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

            await self._repos.signals(session, market=self._market).advance(
                row.id, **self._rollup(tracking, now=now)
            )
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
                planned_risk_usdt=plan.planned_risk_eur * eur_quote_rate_of(plan),
            )
            fields["realized_r"] = ratio(r)
            fields["realized_eur"] = money(r * plan.planned_risk_eur)
            fields["realized_costs_eur"] = self._realized_costs(tracking, now=now)

        if terminal:
            fields["closed_at"] = max((exit_.at for exit_ in tracking.exits), default=now)
            fields["outcome"] = self._outcome(tracking)
        return fields

    def _realized_costs(self, tracking: SignalTracking, *, now: datetime) -> Decimal:
        """M5.1 §10: charge what the trade cost, not what the plan estimated.

        The **one** genuinely market-shaped branch in this loop, and it is a branch
        rather than another accessor because the two cost models do not share a shape:
        crypto pays a maker fee in, a taker fee out and estimated funding; forex pays a
        measured spread on the round trip, a per-leg commission and rollover tripled on
        Wednesday. §2's ledger replaces one with the other; there is no common form to
        reduce them to that would not be a fiction over both.
        """
        plan = tracking.plan
        opened = min((fill.at for fill in tracking.fills), default=plan.created_at)
        closed = max((exit_.at for exit_ in tracking.exits), default=now)

        if isinstance(plan, ForexPlan):
            return self._realized_forex_costs(tracking, plan, opened=opened, closed=closed)

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

    def _realized_forex_costs(
        self, tracking: SignalTracking, plan: ForexPlan, *, opened: datetime, closed: datetime
    ) -> Decimal:
        """Spread, commission and rollover on the legs that actually happened (§16.10).

        The spread charged is the one the **plan** was priced at, carried forward rather
        than re-measured. The trade was entered against that spread; re-pricing it at
        whatever the spread is now would report a cost the owner never paid, which is
        the defect M5.1 §10 introduced realized costs to fix in the first place.
        """
        forex = self._settings.config.forex
        swap = forex.swap_pips_per_night.get(plan.symbol, {})
        costs = forex_realized_costs_eur(
            fills=tuple((fill.price, fill.qty) for fill in tracking.fills),
            exits=tuple((exit_.price, exit_.qty) for exit_ in tracking.exits if exit_.qty > 0),
            pip=plan.pip,
            spread_pips=plan.costs.spread_pips,
            spread_basis=plan.costs.spread_basis,
            eur_quote_rate=plan.eur_quote_rate,
            commission_per_million_quote=forex.commission_per_million_quote,
            rollover_pips_per_night=swap.get("long", Decimal("0")),
            nights=rollover_nights(opened, closed, rollover_hour_utc=forex.rollover_hour_utc),
            credit_favourable_rollover=forex.credit_favourable_rollover,
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

    async def _enforce_daily_loss(self, *, now: datetime) -> list[int]:
        """§7: "checked before every signal AND on every tracker tick".

        The window is the UTC calendar day (owner ruling, M7), so it resets at the
        same instant every stored row is stamped against. A quiet day never lifts
        an existing pause — only ``/resume`` does, and a loss-limit pause needs the
        confirmation button (§3).

        **Per user from M8.1**, and it has to be: the limit is a percentage of
        *somebody's* capital, and there is no longer one capital. Each user's realized
        EUR is divided by their own, compared against the same configured limit.

        **Two rails from M10a, and they answer different questions.** A loss confined
        to one market stops that market and leaves a good day elsewhere alone; a loss
        that is only bearable *because* it is spread across markets still has to stop
        everything. So this runs once per market against ``user_market_pauses``, and
        once combined against the ``users`` row — which keeps exactly the meaning M8.1
        gave it. The operator's ``/pause`` is untouched in ``risk_state``.

        Returns the ids newly paused by **either** rail, deduplicated, so the caller
        tells each person once — a member has no ``/status`` to consult and would
        otherwise just stop hearing from us.
        """
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        limit_pct = self._settings.config.risk.daily_loss_limit_pct
        async with self._database.session() as session:
            accounts = {
                account.telegram_user_id: account
                for account in await self._repos.users(session).approved()
            }

        paused: list[int] = []
        for market in self._settings.config.enabled_markets:
            paused.extend(
                await self._enforce_market_loss(
                    market, accounts=accounts, day_start=day_start, limit_pct=limit_pct, now=now
                )
            )
        paused.extend(
            await self._enforce_combined_loss(
                accounts=accounts, day_start=day_start, limit_pct=limit_pct, now=now
            )
        )
        # Deduplicated, order preserved: one bad day can trip both rails, and two
        # identical "you are paused" messages is a worse experience than one.
        return list(dict.fromkeys(paused))

    async def _enforce_market_loss(
        self,
        market: Market,
        *,
        accounts: dict[int, UserAccount],
        day_start: datetime,
        limit_pct: Decimal,
        now: datetime,
    ) -> list[int]:
        """One market's daily-loss rail, per user (M10a)."""
        async with self._database.session() as session:
            realized_by_user = await self._repos.signals(
                session, market=market
            ).realized_eur_by_user_since(day_start)
            current = await self._repos.user_market_pause(session, market=market).load_many(
                sorted(realized_by_user)
            )

        paused: list[int] = []
        for user_id, realized in sorted(realized_by_user.items()):
            account = accounts.get(user_id)
            if account is None:
                continue
            if already_paused_for_loss(account.pause, now):
                # The combined rail already stopped this person, everywhere. Raising
                # the narrower one on top would change nothing about what they
                # receive and would send a second "you are paused" notice for the
                # same bad day — the per-minute repetition M7 built
                # ``already_paused_for_loss`` to prevent, arriving by a new route.
                continue
            state, loss_pct = self._loss_state(
                realized, account=account, current=current[user_id], limit_pct=limit_pct, now=now
            )
            if state is None:
                continue
            async with self._database.session() as session:
                await self._repos.user_market_pause(session, market=market).save(
                    user_id, state, at=now
                )
                await session.commit()
            paused.append(user_id)
            log.warning(
                "tracker.daily_loss_limit",
                scope="market",
                market=market.value,
                user_id=user_id,
                realized_loss_pct=str(loss_pct),
                limit_pct=str(limit_pct),
                until=None if state.until is None else state.until.isoformat(),
            )
        return paused

    async def _enforce_combined_loss(
        self,
        *,
        accounts: dict[int, UserAccount],
        day_start: datetime,
        limit_pct: Decimal,
        now: datetime,
    ) -> list[int]:
        """The across-markets rail, written to the ``users`` row (M8.1's columns).

        A day that lost 2% in each of two markets is a 4% day, and neither
        per-market rail would have noticed. This is the one that does.
        """
        async with self._database.session() as session:
            realized_by_user = await self._repos.signals(
                session
            ).realized_eur_by_user_since_across_markets(day_start)

        paused: list[int] = []
        for user_id, realized in sorted(realized_by_user.items()):
            account = accounts.get(user_id)
            if account is None:
                # Suspended, rejected or left mid-day. Their signals still resolve —
                # the measurement is theirs — but a pause on a book nothing will be
                # delivered to would be bookkeeping for its own sake.
                continue
            state, loss_pct = self._loss_state(
                realized, account=account, current=account.pause, limit_pct=limit_pct, now=now
            )
            if state is None:
                continue
            async with self._database.session() as session:
                await self._repos.users(session).set_pause(user_id, state, at=now)
                await session.commit()
            paused.append(user_id)
            log.warning(
                "tracker.daily_loss_limit",
                scope="combined",
                user_id=user_id,
                realized_loss_pct=str(loss_pct),
                limit_pct=str(limit_pct),
                until=None if state.until is None else state.until.isoformat(),
            )
        return paused

    @staticmethod
    def _loss_state(
        realized: Sequence[Decimal],
        *,
        account: UserAccount,
        current: PauseState,
        limit_pct: Decimal,
        now: datetime,
    ) -> tuple[PauseState | None, Decimal]:
        """The §7 decision for one book, or ``None`` when nothing new should happen.

        Shared by both rails above so the arithmetic — and, more importantly,
        ``already_paused_for_loss``'s guard against sliding an existing pause's
        expiry forward for ever — has exactly one implementation.
        ``risk.rails.evaluate_daily_loss`` is called unchanged.
        """
        loss_pct = realized_loss_pct(realized, capital_eur=account.capital_eur)
        state = evaluate_daily_loss(
            realized_loss_pct=loss_pct, limit_pct=limit_pct, now=now, current=current
        )
        fresh = (
            state.paused
            and state.reason is PauseReason.DAILY_LOSS_LIMIT
            and not already_paused_for_loss(current, now)
        )
        return (state if fresh else None), loss_pct


__all__ = [
    "OUTCOME_FOR_STATUS",
    "TickResult",
    "TrackerLoop",
    "TrackerRepositories",
    "already_paused_for_loss",
]
