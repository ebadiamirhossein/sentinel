"""Rounding for forex quantities and money (M10b).

A deliberate near-duplicate of ``sentinel/risk/rounding.py``, and the duplication is
the point rather than an oversight: that package is frozen for the live crypto
measurement window, and importing from it would couple a new market's arithmetic to
a module nobody may touch. The semantics differ anyway — forex positions round to
the venue's ``AmountDecimals``, not to a quantity *step*, and there is no tick grid
for a stop to be pushed away along, because a forex plan's levels come from the bid
series rather than from an order book.

The one rule that carries across unchanged, because it is about honesty rather than
about a venue: **quantities only ever floor.** A position is never larger than it was
sized to be, so realized risk is never larger than the budget. Under-risking is fine;
over-risking is not (specs/FOREX.md §7.2).
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")
#: Pip values are small — 0.125 EUR per pip on a 1461-unit EURUSD position, and
#: 0.083 on a yen cross. Quantized to cents they would read as 0.13 and 0.08, and
#: multiplying either by an 18-pip stop would no longer reconcile with the risk
#: figure beside it. Six places is enough that it does.
PIP_VALUE_PLACES = Decimal("0.000001")


def floor_to_decimals(value: Decimal, decimals: int) -> Decimal:
    """Floor to ``decimals`` places — the venue's ``AmountDecimals``."""
    if decimals < 0:
        raise ValueError(f"decimals must not be negative, got {decimals}")
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_FLOOR)


#: Costs are quantized finer than cents, and at EUR 200 they have to be. A 1.1-pip
#: spread on a 1461-unit EURUSD position is EUR 0.1375; rounded to cents it becomes
#: 0.14, and net RR computed from that comes out 1.35 where the exact figure is 1.36.
#: The rounding would be a tenth of the quantity being measured. Four places keeps
#: the value exact for every fixture in this milestone, so a card still reconciles by
#: hand and the gate is not decided by a rounding step.
COST_PLACES = Decimal("0.0001")


def money(value: Decimal) -> Decimal:
    """Quantize to cents for display and storage."""
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def cost_money(value: Decimal) -> Decimal:
    """Quantize a cost. Finer than cents — see :data:`COST_PLACES`."""
    return value.quantize(COST_PLACES, rounding=ROUND_HALF_UP)


def percent(value: Decimal) -> Decimal:
    """2dp, trailing zeros trimmed, integral results re-quantized (M10c).

    The trimming matters on a card: an untrimmed ``Decimal`` normalizes ``20.00`` to
    ``2E+1``, which is a correct number nobody can read. Same behaviour as
    ``sentinel/risk/rounding.percent``, and a copy for the same reason the rest of this
    module is one — see the module docstring.
    """
    quantized = value.quantize(CENTS, rounding=ROUND_HALF_UP)
    normalized = quantized.normalize()
    if normalized == normalized.to_integral():
        return normalized.quantize(Decimal(1))
    return normalized


def pip_value(value: Decimal) -> Decimal:
    """Quantize a per-pip amount finely enough that pips x value still reconciles."""
    return value.quantize(PIP_VALUE_PLACES, rounding=ROUND_HALF_UP).normalize()


__all__ = [
    "CENTS",
    "COST_PLACES",
    "PIP_VALUE_PLACES",
    "cost_money",
    "floor_to_decimals",
    "money",
    "percent",
    "pip_value",
]
