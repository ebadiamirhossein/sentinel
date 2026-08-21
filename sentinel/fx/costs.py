"""Costs and net reward-to-risk for forex (FOREX.md §7.3, §7.4, §7.5).

**The correction that shapes this module (spec defect #16, owner ruling 2026-08-21).**

§7.4 computes net RR by subtracting a cost-in-R from gross: 1.1 pips on an 18-pip
stop is 0.061R, so a gross 1.5 nets 1.439. That is not what a round-trip spread does.

§7.5 settles that features and levels come from the **bid** series and that execution
is modelled asymmetrically — a long enters at ask and exits at bid, a short the
reverse. Work that through with bid-referenced levels and one spread ``s``:

* a long enters at ``bid + s`` and stops out at the bid stop, so the **loss is
  ``risk + s``**;
* and it takes profit at the bid target, so the **gain is ``reward - s``**.

One spread, two effects — it widens the risk *and* shrinks the reward. Subtracting
``s/risk`` from the gross multiple captures neither correctly. The shape here is also
the one ``sentinel/risk/costs.py`` has used for crypto since M4: costs that fall on
the losing side go in the denominator, costs paid on the way out of a winner come off
the numerator. Forex was the odd one out, not this.

What that costs, on an 18-pip stop from a gross 1.5 (and the report carries the full
table): EURUSD nets **1.356**, not §7.4's 1.439, and needs gross **1.653** rather
than 1.561. GBPUSD at 21:00 nets **0.500**, not 0.833, and needs gross **3.167**.
§7.4's qualitative conclusion survives — any positive cost sinks a gross 1.5 — but
the required uplift is roughly two and a half times what it claimed.

**There is no funding field here and no liquidation field anywhere in this package.**
Swap/rollover is forex's own concept and is named for itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sentinel.analyst.models import Direction
from sentinel.fx.rounding import cost_money
from sentinel.fx.sizing import ForexSizing

#: ``datetime.weekday()``. Swap is charged at the rollover hour Monday to Friday and
#: **tripled on Wednesday**, which is how the market settles the coming weekend.
WEDNESDAY = 2
SATURDAY = 5
MILLION = Decimal("1000000")


def ratio(value: Decimal) -> Decimal:
    """Quantize an R multiple to 2dp, as every RR figure in this system is shown."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class ExecutionLeg(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


def execution_price(
    *, direction: Direction, leg: ExecutionLeg, bid: Decimal, ask: Decimal
) -> Decimal:
    """Which side of the book a leg actually transacts on (§7.5).

    Four cases and they are not symmetric within a direction, which is the whole
    point: a long **buys at the ask** and **sells at the bid**, so it crosses the
    spread once on the way in and never gets it back. A short is the mirror.

    Explicit and tested because getting it wrong shifts every level by the spread —
    one pip on EURUSD, and twelve at rollover on GBPUSD.
    """
    if direction is Direction.LONG:
        return ask if leg is ExecutionLeg.ENTRY else bid
    return bid if leg is ExecutionLeg.ENTRY else ask


def rollover_nights(created_at: datetime, expires_at: datetime, *, rollover_hour_utc: int) -> int:
    """How many swap charges fall inside ``(created_at, expires_at]``.

    Charged at the rollover hour on weekdays and **tripled on Wednesday**, which is
    the market's way of settling Saturday and Sunday in advance. Saturday and Sunday
    are skipped rather than charged, because the market is shut — Wednesday's triple
    is what covers them, and charging both would double-count the weekend.
    """
    nights = 0
    at = created_at.replace(hour=rollover_hour_utc, minute=0, second=0, microsecond=0)
    if at <= created_at:
        at += timedelta(days=1)
    while at <= expires_at:
        weekday = at.weekday()
        if weekday < SATURDAY:
            nights += 3 if weekday == WEDNESDAY else 1
        at += timedelta(days=1)
    return nights


class ForexCosts(BaseModel):
    """What a forex round trip costs before it makes anything.

    Note the absences, which are load-bearing rather than incidental (§7.6, and the
    owner's ruling of 2026-08-21): **no funding field and no liquidation field**. A
    perpetual-futures funding rate does not exist in this market and neither does a
    per-position liquidation price, so there is nowhere here to put either.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The spread charged, in pips. Measured, never configured.
    spread_pips: Decimal
    #: Where that number came from, in words, travelling with it.
    spread_basis: str
    #: One full round trip's worth. It widens the loss and shrinks the gain alike.
    spread_cost_eur: Decimal

    #: Per leg, from config. Saxo's standard FX spot pricing is spread-only, so this
    #: is 0 by default — a **configured** zero for an account-specific fee, not a
    #: missing measurement standing in as one.
    entry_commission_eur: Decimal = Decimal("0")
    exit_commission_eur: Decimal = Decimal("0")

    #: Swap. Signed: positive is paid, negative is received.
    rollover_nights: int = 0
    rollover_eur: Decimal = Decimal("0")
    #: What net RR actually charges. With ``credit_favourable_rollover`` false this is
    #: ``max(0, rollover_eur)``, so a swap *credit* can never be the reason a plan
    #: clears the gate — the same rule, and the same reasoning, as crypto's funding.
    rollover_charged_eur: Decimal = Decimal("0")

    #: Spread + both commissions + charged rollover. The worst realistic round trip.
    total_eur: Decimal = Decimal("0")
    #: ``total_eur`` as a percentage of the planned risk.
    cost_pct_of_risk: Decimal = Decimal("0")


def estimate_costs(
    sizing: ForexSizing,
    *,
    spread_pips: Decimal,
    spread_basis: str,
    commission_per_million_quote: Decimal = Decimal("0"),
    rollover_pips_per_night: Decimal = Decimal("0"),
    nights: int = 0,
    credit_favourable_rollover: bool = False,
) -> ForexCosts:
    """Price one round trip on a sized position.

    ``spread_pips`` is the **measured** spread — the current one for a live decision,
    or this hour-of-day's median when pricing an expected cost. Which it is travels
    with it in ``spread_basis`` rather than being inferred later.
    """
    per_pip = sizing.pip_value_eur
    spread_cost = cost_money(spread_pips * per_pip)

    commission_per_leg = cost_money(
        sizing.notional_quote / MILLION * commission_per_million_quote / sizing.eur_quote_rate
    )
    rollover = cost_money(rollover_pips_per_night * per_pip * nights)
    charged = rollover if credit_favourable_rollover else max(Decimal("0"), rollover)

    total = cost_money(spread_cost + commission_per_leg * 2 + charged)
    return ForexCosts(
        spread_pips=spread_pips,
        spread_basis=spread_basis,
        spread_cost_eur=spread_cost,
        entry_commission_eur=commission_per_leg,
        exit_commission_eur=commission_per_leg,
        rollover_nights=nights,
        rollover_eur=rollover,
        rollover_charged_eur=charged,
        total_eur=total,
        cost_pct_of_risk=(
            (total * Decimal("100") / sizing.risk_eur).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if sizing.risk_eur > 0
            else Decimal("0")
        ),
    )


def net_rr(gross_rr: Decimal, *, risk_eur: Decimal, costs: ForexCosts) -> Decimal:
    """Reward-to-risk after costs, with the spread on **both** sides.

        net = (gross x risk - spread - exit_commission)
              / (risk + spread + entry_commission + stop_exit_commission + rollover)

    The spread appears twice because it happens once and has two effects: entering at
    the ask makes the loss ``risk + s`` and the gain ``reward - s``. See the module
    docstring for why §7.4's subtraction is not equivalent, and by how much.

    The commission on a stop-out is charged in the denominator and the one on a
    take-profit in the numerator — the leg that is paid is the leg that is charged,
    exactly as ``sentinel/risk/costs.py`` does it for crypto.
    """
    net_risk = (
        risk_eur
        + costs.spread_cost_eur
        + costs.entry_commission_eur
        + costs.exit_commission_eur
        + costs.rollover_charged_eur
    )
    if net_risk <= 0:  # pragma: no cover — risk_eur > 0 for any sized position
        return Decimal("0")
    net_reward = gross_rr * risk_eur - costs.spread_cost_eur - costs.exit_commission_eur
    return ratio(net_reward / net_risk)


def gross_rr_needed(target_net_rr: Decimal, *, risk_eur: Decimal, costs: ForexCosts) -> Decimal:
    """The gross multiple a setup needs to clear ``target_net_rr`` after costs.

    Inverting :func:`net_rr` with the commissions folded in gives

        gross = target + (1 + target) x cost / risk

    which is why the uplift is larger than §7.4's ``target + cost / risk``: the cost
    lands on the reward *and* on the risk, so it is charged ``1 + target`` times, not
    once. At a 1.5 target that is two and a half times the naive figure.
    """
    if risk_eur <= 0:  # pragma: no cover — risk_eur > 0 for any sized position
        return target_net_rr
    net_risk_extra = (
        costs.spread_cost_eur
        + costs.entry_commission_eur
        + costs.exit_commission_eur
        + costs.rollover_charged_eur
    )
    numerator_extra = costs.spread_cost_eur + costs.exit_commission_eur
    return ratio((target_net_rr * (risk_eur + net_risk_extra) + numerator_extra) / risk_eur)


__all__ = [
    "MILLION",
    "SATURDAY",
    "WEDNESDAY",
    "ExecutionLeg",
    "ForexCosts",
    "estimate_costs",
    "execution_price",
    "gross_rr_needed",
    "net_rr",
    "ratio",
    "rollover_nights",
]
