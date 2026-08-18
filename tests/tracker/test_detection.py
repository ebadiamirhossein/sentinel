"""Reading events out of candles — the wick, the gap, and the ambiguous bar.

Detection is where the choice of price source shows up. Each test below fails
under a 60-second mark-price poll and passes on 1m high/low, which is the whole
argument for the design: a poll sees the close, and the close is not where the
order filled.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sentinel.bot.models import SignalStatus
from sentinel.risk.models import TradePlan
from sentinel.tracker.detect import observe
from sentinel.tracker.models import EventKind

from .conftest import LATER, candle, fill, tracking


def kinds(events: tuple[object, ...]) -> list[str]:
    return [event.kind.value for event in events]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Fills
# --------------------------------------------------------------------------- #


def test_a_wick_fills_the_rung_even_though_the_candle_closed_above_it(
    plan: TradePlan,
) -> None:
    """The case the mark price misses. Low 83.05 reaches the 83.10 limit; close
    83.35 does not. A poll at either end of this minute sees no fill at all."""
    bar = candle(open="83.40", high="83.45", low="83.05", close="83.35")
    events = observe(tracking(plan), candles=[bar], now=LATER)
    assert kinds(events) == [EventKind.ENTRY_FILLED.value]
    assert events[0].index == 0


def test_price_that_never_reaches_the_rung_fills_nothing(plan: TradePlan) -> None:
    bar = candle(open="83.40", high="83.60", low="83.15", close="83.50")
    assert observe(tracking(plan), candles=[bar], now=LATER) == ()


def test_one_candle_can_sweep_the_whole_ladder(plan: TradePlan) -> None:
    """A flush through the zone fills every rung, in rung order."""
    bar = candle(open="83.40", high="83.45", low="82.05", close="82.30")
    events = observe(tracking(plan), candles=[bar], now=LATER)
    assert kinds(events) == [EventKind.ENTRY_FILLED.value] * 3
    assert [event.index for event in events] == [0, 1, 2]


def test_a_rung_fills_at_its_limit_price_not_at_the_better_one_a_gap_gave(
    plan: TradePlan,
) -> None:
    """A gap through a buy limit really does fill better. Crediting the plan for
    that windfall would flatter every gapped setup in the measured record, so the
    rung is accounted at the price it asked for."""
    bar = candle(open="82.00", high="82.10", low="81.90", close="82.00")
    events = observe(tracking(plan), candles=[bar], now=LATER)
    assert events[0].price == plan.entries[0].price == Decimal("83.10")


def test_an_already_filled_rung_is_not_reported_again(plan: TradePlan) -> None:
    state = tracking(plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(plan, 0),))
    bar = candle(open="83.20", high="83.30", low="82.55", close="82.60")
    events = observe(state, candles=[bar], now=LATER)
    assert [event.index for event in events] == [1]


# --------------------------------------------------------------------------- #
# Stops — the one leg the candle may move
# --------------------------------------------------------------------------- #


def test_a_stop_fills_at_the_worse_of_its_price_and_the_gap_it_opened_through(
    plan: TradePlan,
) -> None:
    """A long's stop at 81.20 with the bar opening at 80.40 fills at 80.40. That
    extra 0.80 is a real loss the owner takes, and rounding it away would make
    every gap-down look like a clean stop-out.

    The gap also fills the rungs it passed on the way: rungs 2 and 3 are resting
    limit orders at 82.60 and 82.10, and a market that trades through them does
    fill them. Reporting only the stop would understate the position that was
    actually carried into it — and understate the loss.
    """
    state = tracking(plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(plan, 0),))
    bar = candle(open="80.40", high="80.60", low="80.10", close="80.30")
    events = observe(state, candles=[bar], now=LATER)
    assert kinds(events) == [
        EventKind.ENTRY_FILLED.value,
        EventKind.ENTRY_FILLED.value,
        EventKind.STOPPED.value,
    ]
    assert events[-1].price == Decimal("80.40")


def test_a_stop_reached_without_a_gap_fills_at_the_stop(plan: TradePlan) -> None:
    state = tracking(plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(plan, 0),))
    bar = candle(open="81.90", high="81.95", low="81.15", close="81.60")
    events = observe(state, candles=[bar], now=LATER)
    assert events[-1].kind is EventKind.STOPPED
    assert events[-1].price == plan.stop == Decimal("81.20")


def test_one_flush_can_fill_the_whole_ladder_and_stop_it_out(plan: TradePlan) -> None:
    """The -1.00R bar, and the reason fills are reported before the stop.

    Price falls from 83.40 through every rung and on through 81.20. In that order
    the limits fill and *then* the stop triggers, so the owner carried the full
    ladder into the stop-out. Reporting the stop alone would record -0.40R for a
    trade that lost the whole budget.
    """
    bar = candle(open="83.40", high="83.45", low="81.10", close="81.30")
    events = observe(tracking(plan), candles=[bar], now=LATER)
    assert kinds(events) == [EventKind.ENTRY_FILLED.value] * 3 + [EventKind.STOPPED.value]
    assert [event.index for event in events[:3]] == [0, 1, 2]


def test_the_live_stop_is_used_once_tp1_has_moved_it(plan: TradePlan) -> None:
    """After §5's breakeven move, a retrace to 82.55 is a stop-out even though it
    is nowhere near the plan's 81.20."""
    state = tracking(
        plan,
        status=SignalStatus.FILLED,
        fills=tuple(fill(plan, i) for i in range(3)),
        stop_price=plan.avg_fill_price,
    )
    bar = candle(open="82.90", high="82.95", low="82.50", close="82.70")
    events = observe(state, candles=[bar], now=LATER)
    assert kinds(events) == [EventKind.STOPPED.value]
    assert events[0].price == Decimal("82.55")


# --------------------------------------------------------------------------- #
# The ambiguous bar, and ordering across bars
# --------------------------------------------------------------------------- #


def test_a_bar_covering_both_the_stop_and_a_target_is_read_as_the_stop(
    plan: TradePlan,
) -> None:
    """Intrabar order is unknowable from OHLC. Assuming the target won would turn
    losses into wins in the record the whole system exists to produce, so the loss
    is assumed — the same bias as flooring quantities and pricing exits as taker.
    """
    state = tracking(plan, status=SignalStatus.FILLED, fills=tuple(fill(plan, i) for i in range(3)))
    bar = candle(open="83.00", high="85.50", low="81.10", close="84.00")
    events = observe(state, candles=[bar], now=LATER)
    assert kinds(events) == [EventKind.STOPPED.value]


def test_events_in_different_bars_resolve_in_time_order(plan: TradePlan) -> None:
    """A target at 12:05 and a stop at 12:40 are not ambiguous at all, and the
    conservative within-bar rule must not be applied across bars."""
    state = tracking(plan, status=SignalStatus.FILLED, fills=tuple(fill(plan, i) for i in range(3)))
    first = candle(at=LATER, open="84.00", high="85.30", low="83.90", close="85.10")
    second = candle(
        at=LATER + timedelta(minutes=35), open="84.00", high="84.10", low="81.10", close="81.30"
    )
    events = observe(state, candles=[first, second], now=LATER)
    assert kinds(events) == [EventKind.TP_HIT.value, EventKind.STOPPED.value]
    assert events[0].at < events[1].at


def test_nothing_is_detected_after_a_stop_inside_the_same_window(plan: TradePlan) -> None:
    """The position is gone; later bars in the same window cannot take a target."""
    state = tracking(plan, status=SignalStatus.FILLED, fills=tuple(fill(plan, i) for i in range(3)))
    first = candle(at=LATER, open="82.00", high="82.10", low="81.10", close="81.30")
    second = candle(
        at=LATER + timedelta(minutes=5), open="82.00", high="85.30", low="82.00", close="85.30"
    )
    events = observe(state, candles=[first, second], now=LATER)
    assert kinds(events) == [EventKind.STOPPED.value]


def test_a_target_needs_a_position(plan: TradePlan) -> None:
    """Price running straight up through TP1 without ever touching the ladder is
    a setup that never triggered, not a winner."""
    bar = candle(open="83.50", high="85.50", low="83.45", close="85.40")
    assert observe(tracking(plan), candles=[bar], now=LATER) == ()


# --------------------------------------------------------------------------- #
# Invalidation — closes only, and only before a fill
# --------------------------------------------------------------------------- #


def test_invalidation_needs_a_close_beyond_the_level(plan: TradePlan) -> None:
    """§4: "1h close 81.05 < 81.40". PROMPTS §2 rule e tells the analyst to treat
    wick-only breaches as noise, so a wick through 81.40 that closes above it is
    exactly the sweep the analyst placed the stop beyond."""
    wick = candle(open="81.60", high="81.70", low="81.05", close="81.55")
    assert observe(tracking(plan), candles=[], closed_1h=[wick], now=LATER) == ()

    broken = candle(open="81.60", high="81.65", low="81.00", close="81.05")
    events = observe(tracking(plan), candles=[], closed_1h=[broken], now=LATER)
    assert kinds(events) == [EventKind.INVALIDATED.value]
    assert events[0].reference_price == Decimal("81.05")


def test_invalidation_is_not_watched_once_a_rung_has_filled(plan: TradePlan) -> None:
    """After entry the stop governs (machine.py rule 1). Honouring both would
    close the same size twice."""
    state = tracking(plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(plan, 0),))
    broken = candle(open="81.60", high="81.65", low="81.30", close="81.05")
    events = observe(state, candles=[], closed_1h=[broken], now=LATER)
    assert EventKind.INVALIDATED.value not in kinds(events)


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #


def test_an_unfilled_signal_expires_at_its_ttl(plan: TradePlan) -> None:
    events = observe(tracking(plan), candles=[], now=plan.expires_at)
    assert kinds(events) == [EventKind.EXPIRED.value]
    assert events[0].at == plan.expires_at


def test_a_signal_that_filled_in_this_very_window_does_not_also_expire(
    plan: TradePlan,
) -> None:
    """The bar that filled rung 1 arrives on the same tick that crosses the TTL.
    §5's time stop is "EXPIRES if no entry fill within entry_ttl" — there was one.
    """
    bar = candle(open="83.40", high="83.45", low="83.05", close="83.35")
    events = observe(tracking(plan), candles=[bar], now=plan.expires_at)
    assert kinds(events) == [EventKind.ENTRY_FILLED.value]


def test_a_filled_signal_never_expires(plan: TradePlan) -> None:
    state = tracking(plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(plan, 0),))
    assert observe(state, candles=[], now=plan.expires_at + timedelta(days=3)) == ()


# --------------------------------------------------------------------------- #
# The short mirror
# --------------------------------------------------------------------------- #


def test_a_short_fills_when_price_rises_into_the_zone(short_plan: TradePlan) -> None:
    """Every comparison in detection flips with the direction; a long-only reading
    of "did price reach the level" would never fill a short at all."""
    bar = candle(open="83.40", high="83.75", low="83.35", close="83.60")
    events = observe(tracking(short_plan), candles=[bar], now=LATER)
    assert kinds(events) == [EventKind.ENTRY_FILLED.value]
    assert events[0].price == short_plan.entries[0].price == Decimal("83.70")


def test_a_short_stops_out_when_price_rises_through_the_stop(short_plan: TradePlan) -> None:
    """Mirror of the long: the rungs above fill on the way up, then the stop."""
    state = tracking(short_plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(short_plan, 0),))
    bar = candle(open="85.00", high="85.70", low="84.90", close="85.65")
    events = observe(state, candles=[bar], now=LATER)
    assert events[-1].kind is EventKind.STOPPED
    assert events[-1].price == short_plan.stop == Decimal("85.60")


def test_a_short_stop_gapping_up_fills_worse(short_plan: TradePlan) -> None:
    state = tracking(short_plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(short_plan, 0),))
    bar = candle(open="86.40", high="86.60", low="86.20", close="86.50")
    events = observe(state, candles=[bar], now=LATER)
    assert events[-1].kind is EventKind.STOPPED
    assert events[-1].price == Decimal("86.40")


def test_a_short_invalidates_on_a_close_above_the_level(short_plan: TradePlan) -> None:
    broken = candle(open="83.00", high="84.00", low="82.90", close="83.90")
    events = observe(tracking(short_plan), candles=[], closed_1h=[broken], now=LATER)
    assert kinds(events) == [EventKind.INVALIDATED.value]


# --------------------------------------------------------------------------- #
# The window a first tick covers — found by the first live run
# --------------------------------------------------------------------------- #


def test_events_come_back_in_the_order_they_happened(plan: TradePlan) -> None:
    """Invalidation is read from 1h closes and fills from 1m bars, in separate
    passes. They did not *happen* in separate passes.

    The live run that found this: a signal published two hours before the tracker
    first looked at it. Price had fallen through the ladder, through the stop, and
    on down past the invalidation level. Reporting the invalidation first made it a
    cancelled signal; in fact it had filled and stopped out, which is -1.00R and a
    completely different row in the statistics.
    """
    late_break = candle(
        at=LATER + timedelta(hours=2), open="81.60", high="81.65", low="81.00", close="81.05"
    )
    early_fill = candle(at=LATER, open="83.40", high="83.45", low="83.05", close="83.35")

    events = observe(tracking(plan), candles=[early_fill], closed_1h=[late_break], now=LATER)

    assert [event.kind for event in events] == [
        EventKind.ENTRY_FILLED,
        EventKind.INVALIDATED,
    ]
    assert events[0].at < events[1].at


def test_a_stop_still_wins_inside_a_single_bar(plan: TradePlan) -> None:
    """Sorting must not disturb the within-bar rule: events sharing an instant keep
    the order detection produced, and there the stop comes before the targets."""
    state = tracking(plan, status=SignalStatus.FILLED, fills=tuple(fill(plan, i) for i in range(3)))
    bar = candle(open="83.00", high="85.50", low="81.10", close="84.00")
    assert kinds(observe(state, candles=[bar], now=LATER)) == [EventKind.STOPPED.value]
