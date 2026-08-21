"""The forex entry ladder and its sizing (FOREX.md §16.3).

Follows §3's rules exactly — zone width below ``0.5 x ATR(1h)`` gives one rung at the
midpoint, otherwise three at 40/35/25 of the **risk budget** — with one substitution
and one consequence.

**The substitution.** The collapse trigger is ``MinimumTradeSize`` (1000 base-currency
units on all three pairs, §7.2) rather than a minimum notional in money. Those are not
the same quantity: crypto's floor is a money figure that a price converts into a size,
and forex's is a size the venue will not go below whatever it is worth. Putting a units
figure where a money one is read is the defect class #12 and the ``forex_instruments``
table are both about.

**The consequence, which §7.2 implies and never states.** At €200 capital with 0.75%
risk, the whole position is around a thousand units — so a 40% rung is around four
hundred, well under the venue minimum. **Every ladder collapses to a single rung at
that account size.** That is not worked around and it is not a bug: it is the
``BELOW_MIN_TICKET`` edge §7.2 asked to have measured, arriving one level earlier than
expected, and the rung count is itself a reading on whether €200 is a viable size for
this market.

A copy of :mod:`sentinel.risk.ladder`'s shape rather than an import of it, for the
reason given in :mod:`sentinel.fx.coherence`.

**No tick rounding.** Crypto rounds every rung to the exchange's tick grid. A forex
limit order is placed at a price the venue quotes to a tenth of a pip, and
``ForexInstrument`` already asserts ``pip == tick_size x 10``, so the levels the
analyst gives are placeable as they stand. Rounding them would move a level for no
reason, and every level here comes from the **bid** series (§7.5) where a shift of one
tick is a shift of a tenth of the thing being measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sentinel.analyst.models import Direction, EntryZone
from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.plan import ForexEntryRung
from sentinel.fx.rounding import floor_to_decimals, money
from sentinel.fx.spread import quantize_pips

HUNDRED = Decimal("100")


@dataclass(frozen=True)
class LadderRung:
    """A price and its share of the **risk budget** — before any sizing."""

    price: Decimal
    weight_pct: Decimal


@dataclass(frozen=True)
class SizedLadder:
    """Every rung sized, plus the aggregates the plan and the gate both need."""

    rungs: tuple[ForexEntryRung, ...]
    units: Decimal
    notional_quote: Decimal
    notional_eur: Decimal
    #: Sigma(price x weight/100) — the conservative basis every gate check uses.
    avg_entry: Decimal
    #: What the owner actually averages if every rung fills (units-weighted).
    avg_fill_price: Decimal
    #: What the floored ladder actually risks. Never above the budget.
    risk_eur: Decimal


def build_ladder(
    *,
    zone: EntryZone,
    direction: Direction,
    last_price: Decimal,
    atr: Decimal,
    single_entry_atr_threshold: Decimal,
    weights_pct: tuple[Decimal, ...],
) -> tuple[LadderRung, ...]:
    """§3. ``direction`` is unused for placement — "nearest price" already encodes it."""
    if zone.width < single_entry_atr_threshold * atr:
        return (LadderRung(price=zone.midpoint, weight_pct=HUNDRED),)

    near, far = _order_edges(zone, last_price)
    return tuple(
        LadderRung(price=price, weight_pct=weight)
        for price, weight in zip((near, zone.midpoint, far), weights_pct, strict=True)
    )


def _order_edges(zone: EntryZone, last_price: Decimal) -> tuple[Decimal, Decimal]:
    """Rung 1 is the edge nearest the current price; the last rung is the far edge."""
    if abs(zone.high - last_price) <= abs(zone.low - last_price):
        return zone.high, zone.low
    return zone.low, zone.high


def collapse(rungs: tuple[LadderRung, ...], *, zone: EntryZone) -> tuple[LadderRung, ...] | None:
    """One step of 3 -> 2 -> 1. ``None`` when there is nothing left to collapse.

    Drops the far rung — the smallest weight — and renormalizes the survivors, with the
    last one absorbing the rounding residual so the weights sum to exactly 100. Two
    rungs collapse straight to a single midpoint rung, matching §3's narrow-zone rule.
    """
    if len(rungs) <= 1:
        return None
    if len(rungs) == 2:
        return (LadderRung(price=zone.midpoint, weight_pct=HUNDRED),)

    kept = rungs[:-1]
    total = sum((rung.weight_pct for rung in kept), Decimal(0))
    head = tuple(
        LadderRung(
            price=rung.price,
            weight_pct=(rung.weight_pct / total * HUNDRED).quantize(Decimal("0.01")),
        )
        for rung in kept[:-1]
    )
    residual = HUNDRED - sum((rung.weight_pct for rung in head), Decimal(0))
    return (*head, LadderRung(price=kept[-1].price, weight_pct=residual))


def size_ladder(
    rungs: tuple[LadderRung, ...],
    *,
    zone: EntryZone,
    stop: Decimal,
    instrument: ForexInstrument,
    risk_eur: Decimal,
    eur_quote_rate: Decimal,
    last_price: Decimal,
) -> SizedLadder | None:
    """Size every rung, collapsing while any of them is below the venue minimum.

    ``None`` means even a single rung carrying the whole budget is below
    ``MinimumTradeSize`` — the ``BELOW_MIN_TICKET`` case, and at €200 the common one.

    Units are always **floored** to the venue's amount precision, never rounded up:
    §7.2 is explicit that under-risking is fine and over-risking is not.
    """
    current: tuple[LadderRung, ...] | None = rungs
    while current is not None:
        sized = _size_once(
            current,
            stop=stop,
            instrument=instrument,
            risk_eur=risk_eur,
            eur_quote_rate=eur_quote_rate,
            last_price=last_price,
        )
        if sized is not None:
            return sized
        current = collapse(current, zone=zone)
    return None


def _size_once(
    rungs: tuple[LadderRung, ...],
    *,
    stop: Decimal,
    instrument: ForexInstrument,
    risk_eur: Decimal,
    eur_quote_rate: Decimal,
    last_price: Decimal,
) -> SizedLadder | None:
    sized: list[ForexEntryRung] = []
    for rung in rungs:
        stop_distance = abs(rung.price - stop)
        if stop_distance <= 0:
            return None
        share = risk_eur * rung.weight_pct / HUNDRED
        units = floor_to_decimals(
            share * eur_quote_rate / stop_distance, instrument.amount_decimals
        )
        if units < instrument.min_trade_size:
            return None
        notional_quote = units * rung.price
        sized.append(
            ForexEntryRung(
                price=rung.price,
                weight_pct=rung.weight_pct,
                qty=units,
                notional_eur=money(notional_quote / eur_quote_rate),
                distance_pct=_signed_pct(last_price, rung.price),
                distance_pips=quantize_pips(abs(rung.price - last_price) / instrument.pip),
            )
        )

    total_units = sum((rung.qty for rung in sized), Decimal(0))
    notional_quote = sum((rung.qty * rung.price for rung in sized), Decimal(0))
    actual_risk_quote = sum((rung.qty * abs(rung.price - stop) for rung in sized), Decimal(0))

    # Both averages are quantized to the venue's own price grid — ``tick_size``, a tenth
    # of a pip. Unquantized, ``notional / units`` is a repeating decimal and the card
    # prints 1.167529411764705882352941176, which is not a price and cannot be checked
    # against anything the owner sees in SaxoTraderGO. Rounding here moves a level by at
    # most half a tick, or 0.05 pips, against stops measured in tens of pips.
    tick = instrument.tick_size
    avg_entry = _to_tick(
        sum((rung.price * rung.weight_pct / HUNDRED for rung in sized), Decimal(0)), tick
    )
    avg_fill_price = _to_tick(notional_quote / total_units, tick)
    return SizedLadder(
        rungs=tuple(sized),
        units=total_units,
        notional_quote=notional_quote,
        notional_eur=money(notional_quote / eur_quote_rate),
        avg_entry=avg_entry,
        avg_fill_price=avg_fill_price,
        risk_eur=money(actual_risk_quote / eur_quote_rate),
    )


def _to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Snap a derived price to the venue's grid. ``ForexInstrument`` asserts that the
    grid is ``pip / 10``, so this is always a tenth-of-a-pip figure."""
    return price.quantize(tick, rounding=ROUND_HALF_UP)


def _signed_pct(reference: Decimal, price: Decimal) -> Decimal:
    """Signed, 2dp. Negative is below the reference — a ladder may straddle it."""
    if reference <= 0:
        return Decimal("0.00")
    return ((price - reference) / reference * HUNDRED).quantize(Decimal("0.01"))


__all__ = ["LadderRung", "SizedLadder", "build_ladder", "collapse", "size_ladder"]
