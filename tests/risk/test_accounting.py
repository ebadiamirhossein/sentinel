"""§8.5 — ladder-aware R accounting for partial fills.

Pure math the tracker (M7) will call on every tick. Numbers come from the golden
long ladder: rungs 18.30 @83.10 (40% of risk), 21.73 @82.60 (35%), 24.15 @82.10
(25%), stop 81.20, planned risk 86.9475 USDT.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.analyst.models import Direction
from sentinel.risk.accounting import Exit, Fill, avg_fill_price, open_qty, realized_r

PLANNED_RISK = Decimal("86.9475")
STOP = Decimal("81.20")

RUNG1 = Fill(price=Decimal("83.10"), qty=Decimal("18.30"))
RUNG2 = Fill(price=Decimal("82.60"), qty=Decimal("21.73"))
RUNG3 = Fill(price=Decimal("82.10"), qty=Decimal("24.15"))


def stopped(fills: tuple[Fill, ...]) -> Decimal:
    filled = sum((f.qty for f in fills), Decimal(0))
    return realized_r(
        direction=Direction.LONG,
        fills=fills,
        exits=(Exit(price=STOP, qty=filled),),
        planned_risk_usdt=PLANNED_RISK,
    )


def test_rung_one_only_then_stop_is_minus_040r() -> None:
    """§3's promise to the tracker and the card, now literally true."""
    assert stopped((RUNG1,)) == pytest.approx(Decimal("-0.40"), abs=Decimal("0.01"))


def test_rungs_one_and_two_then_stop_is_minus_075r() -> None:
    assert stopped((RUNG1, RUNG2)) == pytest.approx(Decimal("-0.75"), abs=Decimal("0.01"))


def test_full_ladder_then_stop_is_minus_one_r() -> None:
    assert stopped((RUNG1, RUNG2, RUNG3)) == pytest.approx(Decimal("-1.00"), abs=Decimal("0.01"))


def test_full_ladder_loss_never_exceeds_one_r() -> None:
    """Rounding quantities down can only ever lose *less* than the budget."""
    assert stopped((RUNG1, RUNG2, RUNG3)) >= Decimal("-1")


def test_partial_ladder_then_tp1_full_close() -> None:
    """Rungs 1+2 filled (40.03 SOL, avg 82.8286), all closed at TP1 84.90.

    pnl = 40.03 x (84.90 - 82.8286) = 82.919 USDT → 82.919/86.9475 = +0.95R
    """
    fills = (RUNG1, RUNG2)
    filled = Decimal("40.03")
    assert open_qty(fills, ()) == filled
    assert avg_fill_price(fills) == pytest.approx(Decimal("82.8286"), abs=Decimal("0.0001"))

    r = realized_r(
        direction=Direction.LONG,
        fills=fills,
        exits=(Exit(price=Decimal("84.90"), qty=filled),),
        planned_risk_usdt=PLANNED_RISK,
    )
    assert r == pytest.approx(Decimal("0.95"), abs=Decimal("0.01"))


def test_partial_ladder_then_tp1_scale_out_per_management_plan() -> None:
    """TP1 closes 40% of the *filled* position (§5); the rest stays open."""
    fills = (RUNG1, RUNG2)
    exits = (Exit(price=Decimal("84.90"), qty=Decimal("16.01")),)

    assert open_qty(fills, exits) == Decimal("24.02")
    r = realized_r(
        direction=Direction.LONG, fills=fills, exits=exits, planned_risk_usdt=PLANNED_RISK
    )
    assert r == pytest.approx(Decimal("0.38"), abs=Decimal("0.01"))


def test_short_direction_flips_the_sign() -> None:
    fills = (Fill(price=Decimal("84.10"), qty=Decimal("10")),)
    exits = (Exit(price=Decimal("83.10"), qty=Decimal("10")),)
    r = realized_r(
        direction=Direction.SHORT, fills=fills, exits=exits, planned_risk_usdt=Decimal("10")
    )
    assert r == Decimal("1")  # 10 x 1.00 profit on a 10 USDT risk budget


def test_nothing_filled_is_flat() -> None:
    assert realized_r(
        direction=Direction.LONG, fills=(), exits=(), planned_risk_usdt=PLANNED_RISK
    ) == Decimal("0")
    assert open_qty((), ()) == Decimal("0")
    assert avg_fill_price(()) == Decimal("0")
