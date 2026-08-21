"""The state machine, as one pure function of (status, event).

ARCHITECTURE.md §3:

    PENDING_ENTRY -> PARTIALLY_FILLED -> FILLED -> (TP1_HIT -> ...)
                   | STOPPED | INVALIDATED | EXPIRED

**TP hits are events, not statuses** (owner ruling, M7). ``SignalStatus`` shipped
at M6 without ``TP*_HIT`` and with ``CLOSED``, and ``signals.status`` already holds
those values; a signal stays ``FILLED`` while any size is open and becomes
``CLOSED`` when the last unit closes. The TP hits themselves live in
``signal_exits`` and ``signal_events``, where they can be counted, and the status
column stays a state rather than a partly-ordered history.

Three rules that the spec implies and this file makes explicit, because each one
is a place a plausible reading would be wrong:

1. **Invalidation only cancels an unfilled signal.** §4 words it that way — "❌
   Invalidation triggered ... *before entry* -> signal cancelled". Once a rung has
   filled the position is real and the stop is what closes it; honouring both
   would exit the same size twice.
2. **Expiry only expires an unfilled signal.** §5's time stop is "EXPIRES if no
   entry fill within entry_ttl". A filled position does not stop existing because
   its ladder's clock ran out.
3. **A terminal status absorbs everything.** A fill arriving for a stopped signal
   is not a new position; it is a late detection of something that no longer
   matters. It is refused with a reason rather than silently dropped.

Pure: no clock, no database, no IO. Every decision here is a function of its
arguments, which is what lets the truth table cover it exhaustively.
"""

from __future__ import annotations

from decimal import Decimal

from sentinel.bot.models import TERMINAL_STATUSES, SignalStatus
from sentinel.bot.plans import eur_quote_rate_of, qty_step_of
from sentinel.core.config import ManagementConfig
from sentinel.risk.accounting import realized_r
from sentinel.risk.rounding import floor_to_step, money, ratio
from sentinel.tracker.models import (
    EXIT_EXPIRY,
    EXIT_INVALIDATION,
    EXIT_MANUAL,
    EXIT_STOP,
    EventKind,
    LegExit,
    MarketEvent,
    RungFill,
    SignalTracking,
    TrackerEvent,
    Transition,
)

HUNDRED = Decimal("100")


def target_close_qty(
    tracking: SignalTracking, target_index: int, *, management: ManagementConfig
) -> Decimal:
    """How much §5's management plan closes at this target.

    "TP1: close 40%, move stop to breakeven. TP2: close 35%. TP3: close remainder
    or trail by 1xATR." The percentages are shares of what actually **filled**, not
    of the plan's intended size — a ladder that filled one rung of three closes 40%
    of that rung, not 40% of a position it never had.

    The last target closes whatever is left, so rounding can never strand a
    fraction of a position open forever.
    """
    open_qty = tracking.open_qty
    if target_index >= len(tracking.plan.targets) - 1:
        return open_qty

    share = (management.tp1_close_pct if target_index == 0 else management.tp2_close_pct) / HUNDRED
    step = qty_step_of(tracking.plan)
    wanted = floor_to_step(tracking.filled_qty * share, step)
    return min(wanted, open_qty)


def _realized(tracking: SignalTracking, extra: LegExit | None = None) -> tuple[Decimal, Decimal]:
    """``(R, EUR)`` realized once ``extra`` is included. Gross of costs.

    1R is the plan's **planned** risk budget, converted to USDT with the plan's own
    stored rate — the same basis specs/RISK_ENGINE.md §3 promises the card when it
    says a rung-1-only stop-out is -0.40R.
    """
    plan = tracking.plan
    # ``eur_quote_rate_of`` rather than ``plan.eurusd_rate``: on USDJPY the rate is
    # EURJPY, and reaching for EURUSD there is a ~145x error (§7.1).
    planned_risk_quote = plan.planned_risk_eur * eur_quote_rate_of(plan)
    exits = tuple(exit_.as_exit() for exit_ in tracking.exits)
    if extra is not None:
        exits = (*exits, extra.as_exit())

    r = realized_r(
        direction=plan.direction,
        fills=tuple(fill.as_fill() for fill in tracking.fills),
        exits=exits,
        planned_risk_usdt=planned_risk_quote,
    )
    return ratio(r), money(r * plan.planned_risk_eur)


def _refuse(tracking: SignalTracking, reason: str) -> Transition:
    return Transition(status=tracking.status, applied=False, reason=reason)


def advance(
    tracking: SignalTracking,
    event: MarketEvent,
    *,
    management: ManagementConfig,
) -> Transition:
    """Apply one market event to one signal.

    The branching below *is* the transition table; splitting it into smaller
    helpers per branch is exactly what would hide a missing case.

    Returns the new status, the rows to write, and the journal entry to post. An
    event that changes nothing comes back with ``applied=False`` and a reason
    rather than raising: a tick re-run after a crash replays events that were
    already handled, and that is a normal Tuesday, not an error.
    """
    status = tracking.status

    if status in TERMINAL_STATUSES:
        return _refuse(tracking, f"signal is already {status.value}")

    if event.kind is EventKind.ENTRY_FILLED:
        return _entry_filled(tracking, event)
    if event.kind is EventKind.TP_HIT:
        return _target_hit(tracking, event, management=management)
    if event.kind is EventKind.STOPPED:
        return _closed_out(tracking, event, kind=EXIT_STOP, status=SignalStatus.STOPPED)
    if event.kind is EventKind.CLOSED_MANUALLY:
        return _closed_out(tracking, event, kind=EXIT_MANUAL, status=SignalStatus.CLOSED)
    if event.kind is EventKind.INVALIDATED:
        return _invalidated(tracking, event)
    if event.kind is EventKind.EXPIRED:
        return _expired(tracking, event)

    # LADDER_COMPLETE and NOTE are produced *by* this module and by the bot, never
    # fed into it as market events.
    return _refuse(tracking, f"{event.kind.value} is not a market event")


def _entry_filled(tracking: SignalTracking, event: MarketEvent) -> Transition:
    if event.index in tracking.filled_rungs:
        return _refuse(tracking, f"rung {event.index + 1} already filled")
    if not 0 <= event.index < tracking.rung_count:
        return _refuse(tracking, f"no rung {event.index} on this plan")

    rung = tracking.plan.entries[event.index]
    fill = RungFill(rung_index=event.index, price=event.price, qty=rung.qty, at=event.at)
    filled = tracking.filled_rungs | {event.index}
    complete = len(filled) == tracking.rung_count
    status = SignalStatus.FILLED if complete else SignalStatus.PARTIALLY_FILLED

    events: tuple[TrackerEvent, ...] = (
        TrackerEvent(
            event_key=f"fill:{event.index}",
            kind=EventKind.ENTRY_FILLED,
            at=event.at,
            from_status=tracking.status,
            to_status=status,
            price=event.price,
            detail=f"Entry {event.index + 1} filled",
            payload={
                "rung": str(event.index + 1),
                "weight_pct": str(rung.weight_pct),
                "qty": str(rung.qty),
            },
        ),
    )
    if complete:
        # §4 reports the completed ladder on its own line, with the average the
        # owner actually holds — the quantity-weighted price the plan already
        # stores as ``avg_fill_price``, not §3's risk-weighted ``avg_entry``.
        events = (
            *events,
            TrackerEvent(
                event_key="ladder_complete",
                kind=EventKind.LADDER_COMPLETE,
                at=event.at,
                from_status=tracking.status,
                to_status=status,
                price=tracking.plan.avg_fill_price,
                detail="Ladder complete",
            ),
        )

    return Transition(
        status=status,
        applied=True,
        fill=fill,
        event=events[0],
        extra_events=events[1:],
    )


def _target_hit(
    tracking: SignalTracking, event: MarketEvent, *, management: ManagementConfig
) -> Transition:
    if tracking.status is SignalStatus.PENDING_ENTRY:
        return _refuse(tracking, "no position: a target cannot be taken before an entry fills")
    if event.index in tracking.hit_targets:
        return _refuse(tracking, f"TP{event.index + 1} already taken")
    if not 0 <= event.index < len(tracking.plan.targets):
        return _refuse(tracking, f"no target {event.index} on this plan")
    if tracking.open_qty <= 0:
        return _refuse(tracking, "nothing left open to close")

    qty = target_close_qty(tracking, event.index, management=management)
    exit_ = LegExit(kind=f"TP{event.index + 1}", price=event.price, qty=qty, at=event.at)
    remaining = tracking.open_qty - qty
    status = SignalStatus.CLOSED if remaining <= 0 else tracking.status
    realized, realized_eur = _realized(tracking, exit_)

    # §5: TP1 moves the stop to breakeven. "Breakeven" is the price the owner
    # actually averaged, not §3's risk-weighted gate basis — a stop set at a price
    # they never paid is not breakeven.
    stop_price = tracking.plan.avg_fill_price if event.index == 0 else None

    return Transition(
        status=status,
        applied=True,
        exit=exit_,
        stop_price=stop_price,
        event=TrackerEvent(
            event_key=f"tp:{event.index}",
            kind=EventKind.TP_HIT,
            at=event.at,
            from_status=tracking.status,
            to_status=status,
            price=event.price,
            realized_r=realized,
            realized_eur=realized_eur,
            detail=f"TP{event.index + 1} hit",
            payload={
                "target": str(event.index + 1),
                "qty": str(qty),
                "management": tracking.plan.management_plan,
                "breakeven": str(stop_price) if stop_price is not None else "",
            },
        ),
    )


def _closed_out(
    tracking: SignalTracking, event: MarketEvent, *, kind: str, status: SignalStatus
) -> Transition:
    if not tracking.fills:
        return _refuse(tracking, "no position to close")
    if tracking.open_qty <= 0:
        return _refuse(tracking, "position is already fully closed")

    exit_ = LegExit(kind=kind, price=event.price, qty=tracking.open_qty, at=event.at)
    realized, realized_eur = _realized(tracking, exit_)
    partial = len(tracking.filled_rungs) < tracking.rung_count
    rungs = sorted(index + 1 for index in tracking.filled_rungs)

    return Transition(
        status=status,
        applied=True,
        exit=exit_,
        event=TrackerEvent(
            event_key="stop" if kind == EXIT_STOP else "closed_manually",
            kind=EventKind.STOPPED if kind == EXIT_STOP else EventKind.CLOSED_MANUALLY,
            at=event.at,
            from_status=tracking.status,
            to_status=status,
            price=event.price,
            realized_r=realized,
            realized_eur=realized_eur,
            detail="Stopped" if kind == EXIT_STOP else "Closed manually",
            payload={
                # §4 prints "(partial ladder: only rungs 1-2 filled)" — the reason
                # the loss is -0.72R and not -1.00R, stated on the message rather
                # than left for the owner to reconstruct.
                "partial_ladder": ", ".join(str(rung) for rung in rungs) if partial else "",
                "qty": str(exit_.qty),
            },
        ),
    )


def _invalidated(tracking: SignalTracking, event: MarketEvent) -> Transition:
    if tracking.fills:
        return _refuse(
            tracking,
            "already filled: after entry the stop governs the exit, not the invalidation level",
        )
    return Transition(
        status=SignalStatus.INVALIDATED,
        applied=True,
        exit=LegExit(kind=EXIT_INVALIDATION, price=event.price, qty=Decimal(0), at=event.at),
        event=TrackerEvent(
            event_key="invalidated",
            kind=EventKind.INVALIDATED,
            at=event.at,
            from_status=tracking.status,
            to_status=SignalStatus.INVALIDATED,
            price=event.price,
            realized_r=Decimal(0),
            realized_eur=Decimal(0),
            detail="Invalidation triggered before entry",
            payload={
                "close": str(event.reference_price if event.reference_price else event.price),
                "level": str(tracking.plan.report.invalidation_price or ""),
                "text": tracking.plan.report.invalidation_text,
            },
        ),
    )


def _expired(tracking: SignalTracking, event: MarketEvent) -> Transition:
    if tracking.fills:
        return _refuse(
            tracking,
            "already filled: the time stop expires an unfilled ladder, not a position",
        )
    return Transition(
        status=SignalStatus.EXPIRED,
        applied=True,
        exit=LegExit(kind=EXIT_EXPIRY, price=event.price, qty=Decimal(0), at=event.at),
        event=TrackerEvent(
            event_key="expired",
            kind=EventKind.EXPIRED,
            at=event.at,
            from_status=tracking.status,
            to_status=SignalStatus.EXPIRED,
            realized_r=Decimal(0),
            realized_eur=Decimal(0),
            detail=f"Expired unfilled — {event.cause}" if event.cause else "Expired unfilled",
            payload={
                "expires_at": tracking.plan.expires_at.isoformat(),
                **({"cause": event.cause} if event.cause else {}),
            },
        ),
    )


__all__ = ["advance", "target_close_qty"]
