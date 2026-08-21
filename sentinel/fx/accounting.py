"""What a forex trade actually cost, once the tracker knows the legs (§16.10).

:func:`sentinel.fx.costs.estimate_costs` prices the round trip the **plan** intends;
this prices the one that **happened**. They differ whenever a ladder fills partially,
which is the normal case — and at €200 a forex ladder is a single rung, so the more
common difference here is a position that has paid to get in and not yet to get out.

A mirror of :func:`sentinel.risk.costs.realized_costs_eur`, and a copy rather than an
import for the reason given in :mod:`sentinel.fx.coherence`. The **shape** differs too,
not just the package: crypto pays a maker fee in and a taker fee out and estimates
funding; forex pays a **spread** on the round trip, a commission per leg from config,
and **rollover** — charged at 21:00 UTC and tripled on Wednesday, never called funding.

Two conservatisms, both over-stating cost rather than flattering the trade, and both
inherited deliberately from the crypto model:

* the spread is charged on the **filled** quantity for the round trip as soon as
  anything has filled. A position that is still open has not paid its exit spread yet,
  and pretending otherwise over-states — which is the safe direction (§4.2);
* rollover is charged on the whole filled position for the whole window, ignoring that
  a partial close reduces the size being financed. Modelling that exactly needs a
  position-size timeline the tracker does not keep.

**A signal that never filled costs nothing.** It was not a trade, and a cost charged
against it would put a loss in the statistics for an order that never existed.
"""

from __future__ import annotations

from decimal import Decimal

from sentinel.fx.costs import ForexCosts
from sentinel.fx.rounding import cost_money, money

MILLION = Decimal("1000000")


def realized_costs_eur(
    *,
    fills: tuple[tuple[Decimal, Decimal], ...],
    exits: tuple[tuple[Decimal, Decimal], ...],
    pip: Decimal,
    spread_pips: Decimal,
    spread_basis: str,
    eur_quote_rate: Decimal,
    commission_per_million_quote: Decimal,
    rollover_pips_per_night: Decimal,
    nights: int,
    credit_favourable_rollover: bool,
) -> ForexCosts:
    """Price the legs that actually happened. ``fills``/``exits`` are ``(price, qty)``.

    ``spread_pips`` is the **measured** figure the plan was priced at, carried forward
    rather than re-measured: the trade was entered against that spread, and re-pricing
    it at whatever the spread happens to be now would report a cost the owner never
    paid — the exact defect M5.1 §10 introduced this function to fix for crypto.
    """
    filled_qty = sum((qty for _, qty in fills), Decimal(0))
    if filled_qty <= 0:
        return ForexCosts(
            spread_pips=spread_pips,
            spread_basis=f"{spread_basis} — nothing filled, so nothing was paid",
            spread_cost_eur=Decimal("0"),
        )

    filled_notional_quote = sum((price * qty for price, qty in fills), Decimal(0))
    exit_notional_quote = sum((price * qty for price, qty in exits), Decimal(0))

    # One full round trip's worth on the quantity that filled. §7.5: a long enters at
    # the ask and exits at the bid, so the spread lands once and affects both sides.
    spread_cost = cost_money(spread_pips * pip * filled_qty / eur_quote_rate)

    commission = cost_money(
        (filled_notional_quote + exit_notional_quote)
        / MILLION
        * commission_per_million_quote
        / eur_quote_rate
    )

    rollover = cost_money(rollover_pips_per_night * pip * filled_qty * nights / eur_quote_rate)
    charged = rollover if credit_favourable_rollover else max(Decimal("0"), rollover)

    return ForexCosts(
        spread_pips=spread_pips,
        spread_basis=spread_basis,
        spread_cost_eur=spread_cost,
        entry_commission_eur=commission,
        exit_commission_eur=Decimal("0"),
        rollover_nights=nights,
        rollover_eur=rollover,
        rollover_charged_eur=charged,
        total_eur=cost_money(spread_cost + commission + charged),
        cost_pct_of_risk=Decimal("0"),
    )


def realized_pnl_quote(
    *,
    long: bool,
    fills: tuple[tuple[Decimal, Decimal], ...],
    exits: tuple[tuple[Decimal, Decimal], ...],
) -> Decimal:
    """Realized profit in **quote currency** on the quantity actually closed.

    Separate from the crypto function of the same shape only because the euro
    conversion afterwards uses a different rate — EURJPY on the yen cross. The
    arithmetic itself is market-blind, which is why ``eur_quote_rate_of`` in
    ``bot/plans.py`` is the only branch the tracker needs.
    """
    closed = sum((qty for _, qty in exits), Decimal(0))
    if closed <= 0:
        return Decimal("0")
    filled = sum((qty for _, qty in fills), Decimal(0))
    avg_entry = sum((price * qty for price, qty in fills), Decimal(0)) / filled
    avg_exit = sum((price * qty for price, qty in exits), Decimal(0)) / closed
    move = (avg_exit - avg_entry) if long else (avg_entry - avg_exit)
    return money(move * closed)


__all__ = ["realized_costs_eur", "realized_pnl_quote"]
