"""Sizing, leverage and the liquidation buffer (specs/RISK_ENGINE.md §4).

    risk_eur      = capital_eur x risk_per_trade_pct
    risk_usdt     = risk_eur x eurusd_rate        # rate is USD per EUR (see below)
    qty_i         = risk_usdt x w_i / |p_i - stop|   → floored to the qty step
    notional      = Σ qty_i x p_i
    leverage_raw  = notional_eur / margin_eur
    leverage      = clamp(ceil(leverage_raw), 1, max_leverage), then reduced until
                    1/leverage >= liq_buffer_multiple x stop_distance
    margin_final  = notional_eur / leverage

Two corrections to §4 as written, both owner-approved 2026-08-18 and recorded in
journal/M4_REPORT.md:

1. **EUR→USDT multiplies.** §4 says ``notional_usdt = notional_eur / eurusd_rate``,
   but M1 fetches Frankfurter with ``base=EUR``, so the stored rate is USD *per*
   EUR (1.1593). Dividing would undersize every position by ~26%.
2. **Weights are risk shares, not notional shares.** §4's
   ``qty_i = (notional x w_i)/p_i`` contradicts §3 and §8.5, which promise that a
   rung-1-only stop-out is -0.40R. Under notional weighting rung 1 — nearest price,
   furthest from the stop — carries 51.3% of the risk. §3/§8.5 win.
"""

from __future__ import annotations

from decimal import Decimal

from sentinel.analyst.models import Direction, EntryZone
from sentinel.ingestion.models import InstrumentMeta
from sentinel.risk.ladder import collapse
from sentinel.risk.models import EntryRung, Frozen, LadderRung
from sentinel.risk.rounding import floor_to_step, money, percent, round_to_tick

HUNDRED = Decimal("100")


class SizedLadder(Frozen):
    """A ladder whose every rung clears the exchange minimum after rounding."""

    rungs: tuple[EntryRung, ...]
    notional_usdt: Decimal
    notional_eur: Decimal
    avg_entry: Decimal
    avg_fill_price: Decimal
    risk_usdt: Decimal


def risk_budget_eur(capital_eur: Decimal, risk_per_trade_pct: Decimal) -> Decimal:
    """§4 line 1. The percentage is a percentage — 0.75 means 0.75%."""
    return money(capital_eur * risk_per_trade_pct / HUNDRED)


def weighted_avg_entry(rungs: tuple[LadderRung, ...] | tuple[EntryRung, ...]) -> Decimal:
    """§3's ``E = Σ(price_i x w_i)`` — the basis for every gate check."""
    return sum((rung.price * rung.weight_pct / HUNDRED for rung in rungs), Decimal(0))


def stop_distance_fraction(avg_entry: Decimal, stop: Decimal) -> Decimal:
    """``|E - stop| / E`` as a fraction (0.0178 = 1.78%)."""
    return abs(avg_entry - stop) / avg_entry


def distance_pct(reference: Decimal, price: Decimal, *, signed: bool) -> Decimal:
    """``(price - reference) / reference`` as a human percentage, 2dp.

    Added 2026-08-18 (owner directive, from M6). The signal card shows how far a
    target sits from the weighted entry and how far each rung sits from the last
    price; the bot renders and never computes, so the engine owns both numbers.

    ``signed=False`` returns the magnitude, which is what a **target** distance
    reports: a short's reward is a falling price, and a minus sign in front of it
    reads as a loss. ``signed=True`` keeps the direction, which is what a **rung**
    distance needs: §2 rule 2 bounds both zone edges within 3% of the last price
    but does not force the zone to one side of it, so a ladder can straddle price
    and the sign carries information no renderer could re-derive.

    Quantized with the same ``percent()`` as ``stop_distance_pct`` — a card that
    mixes precisions invites the owner to think one figure is more exact than it
    is, and four decimals of a percentage is precision nobody can act on.
    """
    if reference <= 0:
        raise ValueError(f"distance_pct needs a positive reference price, got {reference}")
    delta = price - reference
    return percent((delta if signed else abs(delta)) * HUNDRED / reference)


def planned_qty(
    *, weight_pct: Decimal, price: Decimal, stop: Decimal, risk_usdt: Decimal
) -> Decimal:
    """The rung's share of the risk budget, converted to base units.

    A rung sitting exactly on the stop can risk nothing, so it sizes to zero and
    is collapsed away rather than dividing by zero.
    """
    distance = abs(price - stop)
    if distance <= 0:
        return Decimal(0)
    return risk_usdt * weight_pct / HUNDRED / distance


def size_ladder(
    *,
    rungs: tuple[LadderRung, ...],
    zone: EntryZone,
    stop: Decimal,
    direction: Direction,
    risk_usdt: Decimal,
    instrument: InstrumentMeta,
    min_rung_notional_usdt: Decimal,
    eurusd_rate: Decimal,
    last_price: Decimal,
) -> SizedLadder | None:
    """Size every rung, collapsing the ladder while any rung is below the minimum.

    ``None`` means even a single rung cannot clear the exchange minimum — the
    account is too small for this setup (§4). ``direction`` is carried for
    readability at the call site; the arithmetic is symmetric.
    """
    current = rungs
    while True:
        sized = _size_once(
            current, stop, risk_usdt, instrument, min_rung_notional_usdt, eurusd_rate, last_price
        )
        if sized is not None:
            return sized
        nxt = collapse(current, zone=zone, tick_size=instrument.tick_size)
        if nxt is None:
            return None
        current = nxt


def _size_once(
    rungs: tuple[LadderRung, ...],
    stop: Decimal,
    risk_usdt: Decimal,
    instrument: InstrumentMeta,
    min_rung_notional_usdt: Decimal,
    eurusd_rate: Decimal,
    last_price: Decimal,
) -> SizedLadder | None:
    from sentinel.risk.ladder import effective_min_notional

    floor_usdt = effective_min_notional(instrument, min_rung_notional_usdt)
    entries: list[EntryRung] = []

    for rung in rungs:
        qty = floor_to_step(
            planned_qty(
                weight_pct=rung.weight_pct,
                price=rung.price,
                stop=stop,
                risk_usdt=risk_usdt,
            ),
            instrument.qty_step,
        )
        notional = qty * rung.price
        if qty <= 0 or notional < floor_usdt:
            return None
        entries.append(
            EntryRung(
                price=rung.price,
                weight_pct=rung.weight_pct,
                qty=qty,
                notional_usdt=notional,
                notional_eur=money(notional / eurusd_rate),
                distance_pct=distance_pct(last_price, rung.price, signed=True),
            )
        )

    notional_usdt = sum((entry.notional_usdt for entry in entries), Decimal(0))
    total_qty = sum((entry.qty for entry in entries), Decimal(0))
    return SizedLadder(
        rungs=tuple(entries),
        notional_usdt=notional_usdt,
        notional_eur=money(notional_usdt / eurusd_rate),
        avg_entry=weighted_avg_entry(tuple(entries)),
        avg_fill_price=round_to_tick(notional_usdt / total_qty, instrument.tick_size),
        risk_usdt=sum((entry.qty * abs(entry.price - stop) for entry in entries), Decimal(0)),
    )


def solve_leverage(
    *,
    notional_eur: Decimal,
    margin_budget_eur: Decimal,
    stop_distance_fraction: Decimal,
    max_leverage: int,
    liq_buffer_multiple: Decimal,
) -> int | None:
    """§4's leverage math plus the liquidation-buffer rule.

    Isolated-margin liquidation sits roughly ``1/leverage`` away (conservative:
    maintenance-margin tiers are ignored in v1). The stop must trigger far before
    it, so leverage is reduced until ``1/leverage >= multiple x stop distance``.
    ``None`` means that is impossible above 1x — reject rather than shave the rule.
    """
    if stop_distance_fraction <= 0:
        return None

    from sentinel.risk.rounding import ceil_to_int

    if margin_budget_eur <= 0:
        # No margin budget configured: start from the cap and let the buffer decide.
        leverage = max_leverage
    else:
        leverage = ceil_to_int(notional_eur / margin_budget_eur)

    leverage = max(1, min(max_leverage, leverage))

    max_by_buffer = int(Decimal(1) / (liq_buffer_multiple * stop_distance_fraction))
    leverage = min(leverage, max_by_buffer)
    if leverage < 1:
        return None
    return leverage
