"""Ladder-aware R accounting, hand-calculated (RISK_ENGINE §8.5, MILESTONES M7).

Same discipline as M4's §8.1 goldens and M6's distance goldens: **the arithmetic
is written out in the docstring before the code is asked**. A golden generated
from the code it tests only proves the code agrees with itself, and this is the
number the card promises the owner in specs/TELEGRAM_UX.md §4.

Every case runs on the M4 golden ladder, produced by the real risk engine:

    rung 1   18.30 @ 83.10   (40% of risk)
    rung 2   21.73 @ 82.60   (35%)
    rung 3   24.15 @ 82.10   (25%)
    stop     81.20      targets 85.20 / 86.60 / 88.90
    planned risk  EUR 75.00  x  1.1593  =  86.9475 USDT   <- 1R

1R is the plan's **planned** risk, not the post-rounding actual (EUR 74.98). That
is deliberate and it is what makes -0.40R come out at -0.40: §3 promises the owner
a share of the budget they chose, and flooring three quantities is the engine's
rounding, not a change to the promise.
"""

from __future__ import annotations

from decimal import Decimal

from sentinel.bot.models import SignalStatus
from sentinel.core.config import AppConfig
from sentinel.risk.models import TradePlan
from sentinel.tracker.machine import advance
from sentinel.tracker.models import EventKind, MarketEvent, SignalTracking

from .conftest import LATER, fill, tracking


def close_at(
    state: SignalTracking, config: AppConfig, kind: EventKind, price: str, index: int = 0
) -> tuple[Decimal, Decimal, SignalTracking]:
    """Apply one closing event; return ``(R, EUR, the state after it)``."""
    result = advance(
        state,
        MarketEvent(kind=kind, at=LATER, index=index, price=Decimal(price)),
        management=config.management,
    )
    assert result.applied, result.reason
    assert result.event is not None and result.exit is not None
    after = state.model_copy(update={"status": result.status, "exits": (*state.exits, result.exit)})
    return result.event.realized_r or Decimal(0), result.event.realized_eur or Decimal(0), after


# --------------------------------------------------------------------------- #
# The contract §3 and §8.5 both state in words
# --------------------------------------------------------------------------- #


def test_rung_one_only_then_stopped_is_minus_zero_point_four_r(
    plan: TradePlan, config: AppConfig
) -> None:
    """The promise on every card with a 3-rung ladder.

        filled     18.30 @ 83.10
        exit       18.30 @ 81.20
        P&L        (81.20 - 83.10) x 18.30 = -1.90 x 18.30 = -34.77 USDT
        R          -34.77 / 86.9475        = -0.3998965...  -> -0.40R
        EUR        -0.3998965 x 75.00      = -29.99

    Rung 1 sits nearest the price and therefore furthest from the stop, which is
    exactly why M4 had to rule that the 40/35/25 weights are shares of **risk**
    and not of notional: on the notional reading this same stop-out is -0.51R and
    the card would be lying by a quarter of a unit of risk.
    """
    state = tracking(plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(plan, 0),))
    r, eur, _ = close_at(state, config, EventKind.STOPPED, "81.20")
    assert r == Decimal("-0.40")
    assert eur == Decimal("-29.99")


def test_rungs_one_and_two_then_stopped_is_minus_zero_point_seven_five_r(
    plan: TradePlan, config: AppConfig
) -> None:
    """40% + 35% of the risk budget, so 75% of it is lost.

    filled     18.30 @ 83.10  +  21.73 @ 82.60   = 40.03 qty
    cost       1520.730 + 1794.898               = 3315.628 USDT
    exit       40.03 @ 81.20                     = 3250.436 USDT
    P&L        3250.436 - 3315.628               = -65.192 USDT
    R          -65.192 / 86.9475                 = -0.7497845... -> -0.75R
    EUR        -0.7497845 x 75.00                = -56.23
    """
    state = tracking(
        plan,
        status=SignalStatus.PARTIALLY_FILLED,
        fills=(fill(plan, 0), fill(plan, 1)),
    )
    r, eur, _ = close_at(state, config, EventKind.STOPPED, "81.20")
    assert r == Decimal("-0.75")
    assert eur == Decimal("-56.23")


def test_the_full_ladder_stopped_out_is_minus_one_r(plan: TradePlan, config: AppConfig) -> None:
    """The whole budget, which is what 1R means.

        filled     64.18 qty for 5298.343 USDT   (avg 82.5544...)
        exit       64.18 @ 81.20 = 5211.416 USDT
        P&L        5211.416 - 5298.343           = -86.927 USDT
        R          -86.927 / 86.9475             = -0.99976...  -> -1.00R

    The 0.024% short of a whole R is the three floored quantities — the same
    EUR 0.02 that makes the card's "actual risk EUR 74.98 (planned EUR 75.00)"
    line honest.
    """
    state = tracking(plan, status=SignalStatus.FILLED, fills=tuple(fill(plan, i) for i in range(3)))
    r, _, _ = close_at(state, config, EventKind.STOPPED, "81.20")
    assert r == Decimal("-1.00")


# --------------------------------------------------------------------------- #
# Partial ladder into profit — §8.5's "rung1+2 + TP1 math correct"
# --------------------------------------------------------------------------- #


def test_rungs_one_and_two_then_tp1(plan: TradePlan, config: AppConfig) -> None:
    """§5 closes 40% at TP1 — of what **filled**, not of the intended position.

        filled     40.03 qty, avg 3315.628 / 40.03 = 82.8285785...
        TP1 closes 40% x 40.03 = 16.012 -> floored to the 0.01 step = 16.01
        P&L        (85.20 - 82.8285785) x 16.01 = 2.3714215 x 16.01 = 37.9664...
        R          37.96645 / 86.9475            = 0.4366...   -> 0.44R
        EUR        0.4366 x 75.00                = 32.75

    Note it is *not* 0.4 x the plan's 1.71R gross: the plan's RR is measured from
    §3's risk-weighted ``avg_entry`` 82.675 over the full ladder, and this trade
    only ever held two rungs at a worse average. The two numbers answering
    different questions is the point of tracking outcomes at all.
    """
    state = tracking(
        plan,
        status=SignalStatus.PARTIALLY_FILLED,
        fills=(fill(plan, 0), fill(plan, 1)),
    )
    r, eur, after = close_at(state, config, EventKind.TP_HIT, "85.20")
    assert r == Decimal("0.44")
    assert eur == Decimal("32.75")
    assert after.open_qty == Decimal("40.03") - Decimal("16.01")


def test_the_full_scale_out_through_all_three_targets(plan: TradePlan, config: AppConfig) -> None:
    """§5's whole management plan, run to the end.

        filled     64.18 qty for 5298.343 USDT, avg = 82.5544250...
        TP1        40% x 64.18 = 25.672 -> 25.67 @ 85.20
                   (85.20 - 82.554425) x 25.67 =  67.9119 USDT  -> 0.78R
        TP2        35% x 64.18 = 22.463 -> 22.46 @ 86.60
                   (86.60 - 82.554425) x 22.46 =  90.8636 USDT  -> cum 1.83R
        TP3        remainder    64.18 - 25.67 - 22.46 = 16.05 @ 88.90
                   (88.90 - 82.554425) x 16.05 = 101.8465 USDT  -> cum 3.00R

    Total 260.622 USDT on an 86.9475 USDT budget. Scaling out banks 3.00R where
    holding the whole position to TP3 would have banked the plan's 4.22R gross —
    which is the trade-off §5's template makes on purpose, and now a measured one.
    """
    state = tracking(plan, status=SignalStatus.FILLED, fills=tuple(fill(plan, i) for i in range(3)))

    r1, _, state = close_at(state, config, EventKind.TP_HIT, "85.20", index=0)
    assert r1 == Decimal("0.78")
    assert state.status is SignalStatus.FILLED, "size is still open after TP1"

    r2, _, state = close_at(state, config, EventKind.TP_HIT, "86.60", index=1)
    assert r2 == Decimal("1.83")

    r3, eur3, state = close_at(state, config, EventKind.TP_HIT, "88.90", index=2)
    assert r3 == Decimal("3.00")
    assert eur3 == Decimal("224.81")  # 2.99746... x 75.00
    assert state.status is SignalStatus.CLOSED
    assert state.open_qty == Decimal("0"), "TP3 closes the remainder — nothing stranded"


def test_a_scale_out_then_a_stop_at_breakeven(plan: TradePlan, config: AppConfig) -> None:
    """The management plan's real shape: TP1 banked, stop to breakeven, then hit.

        TP1        25.67 @ 85.20            ->  67.9119 USDT
        stop (BE)  38.51 @ 82.554425        ->   0.0000 USDT  (exit == avg entry)
        R          67.9119 / 86.9475        =  0.78R

    The breakeven leg contributes exactly nothing, which is what "move stop to
    breakeven" is for — and is only true because §5's breakeven is the price
    actually averaged rather than §3's gate basis.
    """
    state = tracking(plan, status=SignalStatus.FILLED, fills=tuple(fill(plan, i) for i in range(3)))
    _, _, state = close_at(state, config, EventKind.TP_HIT, "85.20", index=0)
    state = state.model_copy(update={"stop_price": plan.avg_fill_price})

    r, _, state = close_at(state, config, EventKind.STOPPED, "82.554425")
    assert r == Decimal("0.78")
    assert state.status is SignalStatus.STOPPED


# --------------------------------------------------------------------------- #
# The mirror, and the manual close
# --------------------------------------------------------------------------- #


def test_the_short_mirror_loses_the_same_zero_point_four_r(
    short_plan: TradePlan, config: AppConfig
) -> None:
    """A short's rung 1 also sits nearest price and furthest from the stop.

        zone 83.70-84.70, stop 85.60 -> rung 1 = 83.70, |83.70 - 85.60| = 1.90
        qty  86.9475 x 0.40 / 1.90   = 18.3047... -> 18.30, the long's mirror
        P&L  (85.60 - 83.70) x 18.30 x -1 = -34.77 USDT  -> -0.40R

    The sign lives in ``realized_pnl_usdt`` and nowhere else; if it leaked into
    the machine this would come back +0.40R and read as a winning stop-out.
    """
    state = tracking(short_plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(short_plan, 0),))
    assert short_plan.entries[0].price == Decimal("83.70")
    assert short_plan.entries[0].qty == Decimal("18.30")

    r, eur, _ = close_at(state, config, EventKind.STOPPED, "85.60")
    assert r == Decimal("-0.40")
    assert eur == Decimal("-29.99")


def test_a_manual_close_records_the_price_the_owner_actually_got(
    plan: TradePlan, config: AppConfig
) -> None:
    """specs/TELEGRAM_UX.md §2: "manual close asks for exit price to record honest
    realized R". Not the target, not the last mark — what they were filled at.

        filled  18.30 @ 83.10
        exit    18.30 @ 84.00
        P&L     0.90 x 18.30      = 16.47 USDT
        R       16.47 / 86.9475   = 0.1894...  -> 0.19R
        EUR     0.1894 x 75.00    = 14.21
    """
    state = tracking(plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(plan, 0),))
    r, eur, after = close_at(state, config, EventKind.CLOSED_MANUALLY, "84.00")
    assert r == Decimal("0.19")
    assert eur == Decimal("14.21")
    assert after.status is SignalStatus.CLOSED


def test_an_unfilled_signal_realizes_nothing(plan: TradePlan, config: AppConfig) -> None:
    """Expiry and invalidation are not losses. A signal that never filled has no
    P&L to report, and reporting -0.00R would put a non-trade in the statistics."""
    for kind in (EventKind.EXPIRED, EventKind.INVALIDATED):
        result = advance(
            tracking(plan),
            MarketEvent(kind=kind, at=LATER, price=Decimal("81.05")),
            management=config.management,
        )
        assert result.applied and result.event is not None
        assert result.event.realized_r == Decimal("0")
        assert result.event.realized_eur == Decimal("0")
