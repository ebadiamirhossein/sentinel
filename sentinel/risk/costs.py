"""Transaction costs (specs/RISK_ENGINE.md §4.2, correction 2026-08-18).

The spec as originally written costed nothing, so every RR figure the gate
reported — and every ``min_rr_tp1`` decision made from one — was gross. On M5's
live ETHUSDT plan that was a ~16% understatement of risk: a 0.45% stop against a
€75 budget implies €17.3k of notional, on which a maker-in / taker-out round trip
is ~€12.

The model, all of it deterministic and none of it optimistic:

    entry_fee    = Σ (qty_i x p_i / rate) x maker_fee      # the limit ladder
    stop_exit    = (Q x stop / rate)      x taker_fee      # part of net RISK
    tp_exit_i    = (Q x TP_i / rate)      x taker_fee      # part of net REWARD
    funding      = ±(notional_eur x funding_rate x settlements)

Entries are maker because the ladder is limit orders. **Every** exit is taker,
including a TP that may well rest as a limit — the same conservative bias that
floors quantities and rounds stops away from the entry. A cost estimate that
flatters the trade is worse than none.

Funding carries a sign: positive rate means longs pay shorts, so it is a cost for
a long and a credit for a short. The signed figure is what the owner sees; the
gate charges ``max(0, funding)`` unless ``costs.credit_favourable_funding`` says
otherwise, because funding flips and must never be the reason a plan approves.

No LLM anywhere in this module (rule zero). No floats.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sentinel.analyst.models import Direction
from sentinel.core.config import CostsConfig
from sentinel.risk.accounting import Exit, Fill
from sentinel.risk.models import EntryRung, PlanCosts, RealizedCosts
from sentinel.risk.rounding import money, percent, ratio

HUNDRED = Decimal("100")


def fee_eur(notional_eur: Decimal, fee_pct: Decimal) -> Decimal:
    """A fee on a notional. ``fee_pct`` is a percentage — 0.05 means 0.05%."""
    return notional_eur * fee_pct / HUNDRED


def funding_settlements(
    *,
    created_at: datetime,
    expires_at: datetime,
    next_funding_time: datetime | None,
    interval_hours: int,
) -> int:
    """Settlements falling inside ``(created_at, expires_at]``.

    Counted by stepping ``interval_hours`` from the exchange's published next
    settlement, so a plan created five minutes before a settlement is charged for
    it and one created five minutes after is not.

    Without a published next settlement the anchor is unknown, so the window is
    divided by the interval instead — the same count on average, and never zero
    for a window that certainly spans one.
    """
    if interval_hours <= 0 or expires_at <= created_at:
        return 0

    step = timedelta(hours=interval_hours)
    if next_funding_time is None:
        return int((expires_at - created_at) / step)

    at = next_funding_time
    # A settlement already in the past is not owed; walk forward to the window.
    while at <= created_at:
        at += step

    count = 0
    while at <= expires_at:
        count += 1
        at += step
    return count


def estimate_funding_eur(
    *,
    notional_eur: Decimal,
    funding_rate: Decimal | None,
    settlements: int,
    direction: Direction,
) -> Decimal:
    """Signed funding over the window: **positive is paid out, negative is received**.

    A positive rate means longs pay shorts, so the sign flips with the direction.
    An unavailable rate is zero here and flagged by the caller — never guessed.
    """
    if funding_rate is None or settlements <= 0:
        return Decimal(0)
    magnitude = notional_eur * funding_rate * settlements
    return magnitude if direction is Direction.LONG else -magnitude


def estimate_costs(
    *,
    entries: tuple[EntryRung, ...],
    stop: Decimal,
    targets: tuple[Decimal, ...],
    direction: Direction,
    notional_eur: Decimal,
    planned_risk_eur: Decimal,
    eurusd_rate: Decimal,
    funding_rate: Decimal | None,
    next_funding_time: datetime | None,
    created_at: datetime,
    expires_at: datetime,
    config: CostsConfig,
) -> PlanCosts:
    """Price the whole round trip for one sized ladder."""
    total_qty = sum((entry.qty for entry in entries), Decimal(0))

    entry_fee = sum(
        (fee_eur(entry.notional_eur, config.maker_fee_pct) for entry in entries), Decimal(0)
    )
    stop_exit_fee = fee_eur(total_qty * stop / eurusd_rate, config.taker_fee_pct)
    tp_exit_fees = tuple(
        fee_eur(total_qty * target / eurusd_rate, config.taker_fee_pct) for target in targets
    )

    settlements = funding_settlements(
        created_at=created_at,
        expires_at=expires_at,
        next_funding_time=next_funding_time,
        interval_hours=config.funding_interval_hours,
    )
    funding = estimate_funding_eur(
        notional_eur=notional_eur,
        funding_rate=funding_rate,
        settlements=settlements,
        direction=direction,
    )
    charged = funding if config.credit_favourable_funding else max(Decimal(0), funding)

    round_trip = money(entry_fee + stop_exit_fee + charged)
    return PlanCosts(
        maker_fee_pct=config.maker_fee_pct,
        taker_fee_pct=config.taker_fee_pct,
        entry_fee_eur=money(entry_fee),
        stop_exit_fee_eur=money(stop_exit_fee),
        tp_exit_fees_eur=tuple(money(fee) for fee in tp_exit_fees),
        funding_rate=funding_rate,
        funding_interval_hours=config.funding_interval_hours,
        funding_settlements=settlements if funding_rate is not None else 0,
        funding_eur=money(funding),
        funding_charged_eur=money(charged),
        funding_available=funding_rate is not None,
        round_trip_cost_eur=round_trip,
        cost_pct_of_risk=(
            percent(round_trip / planned_risk_eur * HUNDRED) if planned_risk_eur > 0 else Decimal(0)
        ),
    )


def realized_costs_eur(
    *,
    direction: Direction,
    fills: tuple[Fill, ...],
    exits: tuple[Exit, ...],
    eurusd_rate: Decimal,
    funding_rate: Decimal | None,
    settlements: int,
    config: CostsConfig,
) -> RealizedCosts:
    """Price the legs that actually happened (M5.1 §10 — M7's tracker calls this).

    ``estimate_costs`` prices the plan; this prices the trade. A ladder that filled
    one rung of three paid one rung of entry fees, and a position still running has
    paid to get in and not yet to get out — both fall out of iterating the real
    fills and exits rather than the intended ones.

    Two deliberate conservatisms, both in the direction of over-stating cost:

    * funding is charged on the **whole** filled notional for the whole window,
      ignoring that a partial close reduces the size being funded. Modelling that
      exactly would need a position-size timeline the tracker does not keep, and a
      cost estimate that flatters the trade is worse than none (§4.2).
    * ``settlements`` is supplied by the caller from the real holding window, and
      the 8h interval it is counted with is itself an estimate (§4.2) — which is
      why it is reported next to the figure everywhere it is shown.

    A signal that never filled costs nothing at all: it was not a trade, and a fee
    charged against it would put a loss in the statistics for an order that never
    existed.
    """
    filled_notional_eur = sum((fill.price * fill.qty for fill in fills), Decimal(0)) / eurusd_rate
    exit_notional_eur = sum((exit_.price * exit_.qty for exit_ in exits), Decimal(0)) / eurusd_rate

    entry_fee = fee_eur(filled_notional_eur, config.maker_fee_pct)
    exit_fee = fee_eur(exit_notional_eur, config.taker_fee_pct)

    funding = estimate_funding_eur(
        notional_eur=filled_notional_eur,
        funding_rate=funding_rate,
        settlements=settlements,
        direction=direction,
    )
    charged = funding if config.credit_favourable_funding else max(Decimal(0), funding)

    return RealizedCosts(
        maker_fee_pct=config.maker_fee_pct,
        taker_fee_pct=config.taker_fee_pct,
        entry_fee_eur=money(entry_fee),
        exit_fee_eur=money(exit_fee),
        filled_notional_eur=money(filled_notional_eur),
        funding_rate=funding_rate,
        funding_settlements=settlements if funding_rate is not None else 0,
        funding_eur=money(funding),
        funding_charged_eur=money(charged),
        funding_available=funding_rate is not None,
        total_eur=money(entry_fee + exit_fee + charged),
    )


def net_rr_multiples(
    *, rr_gross: tuple[Decimal, ...], risk_eur: Decimal, costs: PlanCosts
) -> tuple[Decimal, ...]:
    """Reward-to-risk net of costs, on §2.5's ``avg_entry`` basis.

    Working in EUR off the gross multiple keeps the two figures directly
    comparable — the same entry, the same stop, only costs added:

        net_rr_i = (rr_i x risk_eur - entry_fee - tp_exit_fee_i - funding)
                   / (risk_eur + entry_fee + stop_exit_fee + funding)

    ``net_rr <= rr`` then holds by construction for non-negative costs: the
    numerator only shrinks and the denominator only grows.
    """
    reward_costs = costs.entry_fee_eur + costs.funding_charged_eur
    net_risk = risk_eur + costs.entry_fee_eur + costs.stop_exit_fee_eur + costs.funding_charged_eur
    if net_risk <= 0:  # pragma: no cover — risk_eur > 0 for any sized ladder
        return tuple(Decimal(0) for _ in rr_gross)

    return tuple(
        ratio((gross * risk_eur - reward_costs - exit_fee) / net_risk)
        for gross, exit_fee in zip(rr_gross, costs.tp_exit_fees_eur, strict=True)
    )


__all__ = [
    "estimate_costs",
    "estimate_funding_eur",
    "fee_eur",
    "funding_settlements",
    "net_rr_multiples",
    "realized_costs_eur",
]
