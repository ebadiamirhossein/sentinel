"""Reading market events out of candles. Pure, ordered, and conservative.

**Why candles and not a polled mark price.** The tracker runs every 60s. A mark
price sampled once a minute misses the wick that filled the rung or hit the stop,
and that error is not symmetric: it under-reports stop-outs, so the measured win
rate comes out better than the market was. This module reads 1m high/low over the
window since the last tick instead, which sees the wick, and — because candles are
stored — lets any detection dispute be replayed rather than argued about.

**Invalidation is the exception and reads closed 1h candles.**
specs/TELEGRAM_UX.md §4 words it as a close ("1h close 81.05 < 81.40"), and
specs/PROMPTS.md §2 rule e tells the analyst to treat wick-only breaches as noise
and to define invalidation as a candle close. A tick is not an invalidation.

**Which price each leg is accounted at**, all three biased the same way — never in
the trade's favour:

* an **entry** fills at its own limit price, never at a better one a gap would
  have given. A gap through a limit really does fill better, and crediting the
  plan for a windfall it did not plan would flatter every gapped setup.
* a **target** likewise fills at the target price, not at the better extreme the
  candle reached.
* a **stop** fills at the **worse** of the stop and the candle's open. A gap
  through a stop genuinely fills below it for a long, and that loss is real. This
  is the only leg where the candle can move the price, and it moves it one way.

**Ordering.** Candles are walked in time order, so a target reached at 12:05 and a
stop at 12:40 resolve in that order without anything having to guess. Only *within
one candle* is the order unknowable from OHLC — and there the **stop is assumed to
have come first**, for the same reason as everything above.

**Timestamps.** An event is stamped with its candle's ``open_time``. A 1m
candle gives the instant to within a minute, and taking the open rather than the
close never credits the position with time it did not exist for — the same bias
as everything above.

Pure: no clock (the caller passes ``now``), no database, no IO.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sentinel.analyst.models import Direction
from sentinel.ingestion.models import Candle
from sentinel.tracker.models import EventKind, MarketEvent, SignalTracking


def _touched(direction: Direction, candle: Candle, level: Decimal, *, below: bool) -> bool:
    """Did the candle's range reach ``level``?

    ``below`` describes where the level sits relative to price for this direction:
    a long's entries and stop are below, its targets above; a short mirrors.
    """
    reached_down = candle.low <= level
    reached_up = candle.high >= level
    if direction is Direction.LONG:
        return reached_down if below else reached_up
    return reached_up if below else reached_down


def _stop_fill_price(direction: Direction, candle: Candle, stop: Decimal) -> Decimal:
    """A stop fills at the worse of its own price and the open it gapped through."""
    if direction is Direction.LONG:
        return min(stop, candle.open)
    return max(stop, candle.open)


def observe(
    tracking: SignalTracking,
    *,
    candles: Sequence[Candle],
    closed_1h: Sequence[Candle] = (),
    now: datetime,
) -> tuple[MarketEvent, ...]:
    """Every market event in the window, in the order it happened.

    Nothing here decides what an event *means* — a fill on a stopped signal is
    still reported, and the state machine refuses it. Detection answers "did price
    reach this level", and only that; mixing the two would put the state machine's
    rules in two places.
    """
    plan = tracking.plan
    direction = plan.direction
    events: list[MarketEvent] = []

    # Invalidation first, and only while nothing has filled. It is measured on
    # closed 1h candles, so it does not belong in the 1m walk below, and after a
    # fill the stop is what closes the position (see machine.py rule 1).
    level = plan.report.invalidation_price
    if not tracking.fills and level is not None:
        for candle in closed_1h:
            broken = candle.close < level if direction is Direction.LONG else candle.close > level
            if broken:
                events.append(
                    MarketEvent(
                        kind=EventKind.INVALIDATED,
                        at=candle.open_time,
                        price=candle.close,
                        reference_price=candle.close,
                    )
                )
                break

    seen_rungs = set(tracking.filled_rungs)
    seen_targets = set(tracking.hit_targets)
    stopped = False

    for candle in candles:
        if stopped:
            break

        for index, rung in enumerate(plan.entries):
            if index in seen_rungs:
                continue
            if _touched(direction, candle, rung.price, below=True):
                seen_rungs.add(index)
                events.append(
                    MarketEvent(
                        kind=EventKind.ENTRY_FILLED,
                        at=candle.open_time,
                        index=index,
                        price=rung.price,
                    )
                )

        # The stop is evaluated before the targets and it ends the walk: within one
        # candle the order is unknowable, and assuming the stop lost the race would
        # turn losses into wins in the measured record.
        stop = tracking.live_stop
        if seen_rungs and _touched(direction, candle, stop, below=True):
            events.append(
                MarketEvent(
                    kind=EventKind.STOPPED,
                    at=candle.open_time,
                    price=_stop_fill_price(direction, candle, stop),
                )
            )
            stopped = True
            break

        for index, target in enumerate(plan.targets):
            if index in seen_targets:
                continue
            if seen_rungs and _touched(direction, candle, target, below=False):
                seen_targets.add(index)
                events.append(
                    MarketEvent(
                        kind=EventKind.TP_HIT, at=candle.open_time, index=index, price=target
                    )
                )

    # Expiry last: a ladder that filled inside the window is not an expired one,
    # and the events above have already established whether it did.
    if not seen_rungs and now >= plan.expires_at:
        events.append(MarketEvent(kind=EventKind.EXPIRED, at=plan.expires_at))

    # Chronological, across both timeframes. Invalidation is read from 1h closes
    # and fills from 1m bars, so they are discovered in separate passes but did
    # not *happen* in separate passes — a 1h close that broke the level after the
    # ladder filled must not be applied before the fill. ``sorted`` is stable, so
    # events sharing an instant keep the order above: fills, then the stop, then
    # targets, which is the within-candle rule the module doc explains.
    return tuple(sorted(events, key=lambda event: event.at))


__all__ = ["observe"]
