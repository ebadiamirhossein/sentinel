"""Exchange-grid rounding (§4 "Rounding").

Three rules, each chosen so an error can only ever be conservative:

* **Quantities floor** to the qty step — the position is never larger than sized,
  so the realized risk is never larger than the budget.
* **Entry prices round to the nearest tick** — a limit order is a request, and
  half a tick either way changes nothing material.
* **Stops round away from the entry** — a tick must never *tighten* a stop the
  analyst placed beyond a liquidity sweep (§2 rule 3's whole concern). The extra
  distance is then sized for, so risk stays inside the budget.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from sentinel.analyst.models import Direction


def round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """Nearest tick, ties away from zero (deterministic — never banker's rounding)."""
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


def floor_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal(1), rounding=ROUND_FLOOR) * tick


def ceil_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal(1), rounding=ROUND_CEILING) * tick


def round_stop_to_tick(stop: Decimal, direction: Direction, tick: Decimal) -> Decimal:
    """Away from the entry: down for a long, up for a short."""
    if direction is Direction.LONG:
        return floor_to_tick(stop, tick)
    return ceil_to_tick(stop, tick)


def floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    """Quantities only ever round **down** (§4)."""
    if step <= 0:
        return qty
    return (qty / step).quantize(Decimal(1), rounding=ROUND_FLOOR) * step


def ceil_to_int(value: Decimal) -> int:
    """``ceil_to_step(x, 1)`` from §4's leverage formula."""
    return int(value.quantize(Decimal(1), rounding=ROUND_CEILING))


def money(value: Decimal) -> Decimal:
    """Quantize to cents for display and storage."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def percent(value: Decimal) -> Decimal:
    """Quantize a human-facing percentage to 2dp, trailing zeros trimmed.

    **2dp, not 4 (owner ruling 2026-08-18, from M6).** A percentage on a signal
    card is read, not typed into an order field: `+3.0541%` offers four digits of
    precision the owner cannot act on, and 20.0000% reads as though the liquidation
    estimate were measured rather than `1/leverage`. Prices, euro amounts and
    quantities are untouched — those *do* go into order fields, and their precision
    is the exchange's tick and step grid.

    Trailing zeros are stripped so a round number reads as one (`20%`, not
    `20.00%`), which is why this is done here rather than in the renderer: the card
    shows plan fields verbatim, so the plan has to hold the value the owner should
    see. ``normalize()`` alone would turn 20.00 into ``2E+1`` — which is the same
    number but renders as scientific notation in a card and in JSONB — so an
    integral result is re-quantized to a zero exponent instead.

    Every caller is a display field (`stop_distance_pct`, `liq_distance_pct`,
    `cost_pct_of_risk`, `distance_pct`). No gate decision reads a quantized
    percentage — the liquidation-buffer rule, the ATR bounds and the RR checks all
    run on unrounded fractions, so this changes what is shown and never what is
    approved.
    """
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized == quantized.to_integral_value():
        return quantized.quantize(Decimal(1))
    return quantized.normalize()


def ratio(value: Decimal) -> Decimal:
    """Quantize an R multiple to 2dp (1.51R)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
