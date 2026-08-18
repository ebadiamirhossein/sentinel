"""Entry-ladder construction (specs/RISK_ENGINE.md §3) and the §4 minimum floor.

The analyst supplies a zone; the engine decides the ladder. Deterministically:

* zone width **< 0.5 x ATR(1h)** → one entry at the zone midpoint (100%);
* zone width **>= 0.5 x ATR(1h)** → three rungs, 40% at the edge nearest price,
  35% at the midpoint, 25% at the far (best-priced) edge.

The weights are shares of the **risk budget**, not of notional — owner ruling
2026-08-18, which is what makes §3's "rung 1 only + stop = -0.40R" literally true
(see journal/M4_REPORT.md).
"""

from __future__ import annotations

from decimal import Decimal

from sentinel.analyst.models import Direction, EntryZone
from sentinel.ingestion.models import InstrumentMeta
from sentinel.risk.models import LadderRung
from sentinel.risk.rounding import round_to_tick

HUNDRED = Decimal("100")


def build_ladder(
    *,
    zone: EntryZone,
    direction: Direction,
    last_price: Decimal,
    atr: Decimal,
    tick_size: Decimal,
    single_entry_atr_threshold: Decimal,
    weights_pct: tuple[Decimal, ...],
) -> tuple[LadderRung, ...]:
    """§3. ``direction`` is unused for placement — "nearest price" already encodes it."""
    if zone.width < single_entry_atr_threshold * atr:
        return (LadderRung(price=round_to_tick(zone.midpoint, tick_size), weight_pct=HUNDRED),)

    near, far = _order_edges(zone, last_price)
    prices = (near, zone.midpoint, far)
    return tuple(
        LadderRung(price=round_to_tick(price, tick_size), weight_pct=weight)
        for price, weight in zip(prices, weights_pct, strict=True)
    )


def _order_edges(zone: EntryZone, last_price: Decimal) -> tuple[Decimal, Decimal]:
    """Rung 1 is the edge nearest the current price; rung 3 is the far edge."""
    if abs(zone.high - last_price) <= abs(zone.low - last_price):
        return zone.high, zone.low
    return zone.low, zone.high


def collapse(
    rungs: tuple[LadderRung, ...], *, zone: EntryZone, tick_size: Decimal
) -> tuple[LadderRung, ...] | None:
    """One step of §4's "collapse to fewer rungs (3->2->1) preserving total notional".

    Owner ruling: drop the far rung (the smallest weight) and renormalize the
    survivors; the last survivor sits at the zone midpoint, matching §3's
    narrow-zone rule. ``None`` means there is nothing left to collapse.
    """
    if len(rungs) <= 1:
        return None
    if len(rungs) == 2:
        return (LadderRung(price=round_to_tick(zone.midpoint, tick_size), weight_pct=HUNDRED),)

    kept = rungs[:-1]
    total = sum((rung.weight_pct for rung in kept), Decimal(0))
    head = tuple(
        LadderRung(
            price=rung.price,
            weight_pct=(rung.weight_pct / total * HUNDRED).quantize(Decimal("0.01")),
        )
        for rung in kept[:-1]
    )
    # The last rung absorbs the rounding residual so the weights sum to exactly 100.
    residual = HUNDRED - sum((rung.weight_pct for rung in head), Decimal(0))
    return (*head, LadderRung(price=kept[-1].price, weight_pct=residual))


def effective_min_notional(instrument: InstrumentMeta, floor_usdt: Decimal) -> Decimal:
    """§4 (corrected 2026-08-18): ``max(exchange_minimum, 20 USDT)``, never a fixed 20.

    The exchange minimum is per symbol and can exceed the practicality floor —
    BTCUSDT is 50 USDT, SOLUSDT is 5. Hardcoding 20 would produce rungs Binance
    rejects on BTCUSDT.
    """
    return max(instrument.min_notional, floor_usdt)
