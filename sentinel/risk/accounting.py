"""Ladder-aware R accounting (§3, §8.5) — pure math the M7 tracker calls per tick.

Because rung weights are shares of the **risk budget**, a partial ladder fill
loses exactly its share: rung 1 alone stopping out is -0.40R, rungs 1+2 are
-0.75R. That is the number the card promises the owner.
"""

from __future__ import annotations

from decimal import Decimal

from sentinel.analyst.models import Direction
from sentinel.risk.models import Frozen


class Fill(Frozen):
    """An entry rung that actually filled."""

    price: Decimal
    qty: Decimal


class Exit(Frozen):
    """A close — a target, the stop, or a manual exit."""

    price: Decimal
    qty: Decimal


def filled_qty(fills: tuple[Fill, ...]) -> Decimal:
    return sum((fill.qty for fill in fills), Decimal(0))


def avg_fill_price(fills: tuple[Fill, ...]) -> Decimal:
    """Quantity-weighted average of what actually filled. Nothing filled → 0."""
    total = filled_qty(fills)
    if total <= 0:
        return Decimal(0)
    return sum((fill.price * fill.qty for fill in fills), Decimal(0)) / total


def open_qty(fills: tuple[Fill, ...], exits: tuple[Exit, ...]) -> Decimal:
    return filled_qty(fills) - sum((exit_.qty for exit_ in exits), Decimal(0))


def realized_pnl_usdt(
    *, direction: Direction, fills: tuple[Fill, ...], exits: tuple[Exit, ...]
) -> Decimal:
    """Closed P&L only — open size contributes nothing until it is closed."""
    entry = avg_fill_price(fills)
    sign = Decimal(1) if direction is Direction.LONG else Decimal(-1)
    return sum(((exit_.price - entry) * exit_.qty * sign for exit_ in exits), Decimal(0))


def realized_r(
    *,
    direction: Direction,
    fills: tuple[Fill, ...],
    exits: tuple[Exit, ...],
    planned_risk_usdt: Decimal,
) -> Decimal:
    """Realized P&L expressed in R, where 1R is the plan's **planned** risk budget."""
    if planned_risk_usdt <= 0 or not fills:
        return Decimal(0)
    return realized_pnl_usdt(direction=direction, fills=fills, exits=exits) / planned_risk_usdt
