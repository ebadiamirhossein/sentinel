"""Position sizing for forex (docs/specs/FOREX.md §7.1, §7.2, §7.6).

    units        = risk_eur x rate(EUR->quote_ccy) / stop_distance_in_quote
    notional_eur = units x price_quote / rate(EUR->quote_ccy)

``rate(EUR->quote_ccy)`` is how many quote-currency units one euro buys: EURUSD for
EURUSD and GBPUSD, and **EURJPY** for USDJPY. Frankfurter already serves all three
from the same base=EUR call the crypto path uses, so this adds no dependency — but
the yen cross is a different number, and using EURUSD there is a ~145x error that
sizes a position to roughly nothing and looks like an unremarkable rejection.

**A new module, and not an edit to** ``sentinel/risk/``. Beyond the freeze, the
arithmetic genuinely differs: a pip instead of a tick, a minimum *trade size* in base
units instead of a minimum notional, no lot rounding at all, and no per-position
liquidation price to keep a stop clear of.

**What is deliberately absent.** §7.6: CFD margin is account-level, so there is no
liquidation price and the crypto liquidation-buffer rule "does not apply and must not
be faked". Per the owner's ruling of 2026-08-21 the stronger form applies — there is
no field here that *could* hold a liquidation buffer or a perpetual-futures funding
rate. Absent, not ``None`` and not zero, for the same reason §2.1 refuses a zero
volume. Swap/rollover is a real forex concept and lives in :mod:`sentinel.fx.costs`
under its own name.

**Levels come from the bid series** (§7.5, owner decision 7). Execution asymmetry —
a long enters at ask and exits at bid — is charged as one full round-trip spread in
:mod:`sentinel.fx.costs` rather than shifted into the levels here. Doing it the other
way would move every level by the spread, which is one pip on EURUSD and twelve at
rollover on GBPUSD.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.models import ForexRejection
from sentinel.fx.rounding import floor_to_decimals, money, pip_value

HUNDRED = Decimal("100")

#: What ``max_leverage`` actually rests on, carried on every sized position so the
#: label cannot be dropped by a renderer that did not know to add it (§7.6).
#: journal/M10b_SPIKE.md §4 confirms instrument details carry no ``MarginRates`` and
#: no ``MarginTiers``, so v1's "read the venue's actual leverage figure" is not
#: satisfiable and this is an assumption rather than a measurement.
ESMA_ASSUMPTION = (
    "documented assumption: ESMA retail cap of 30:1 on major pairs, from config — "
    "Saxo's instrument details carry no MarginRates or MarginTiers to read"
)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ForexSizing(Frozen):
    """A sized forex position. Everything a card would need, and nothing it must not.

    Note what has no field: no liquidation price, no liquidation buffer, no funding
    rate. See the module docstring — this is §7.6 plus the owner's stronger reading
    of §2.1, and it is asserted by a test rather than left to habit.
    """

    symbol: str
    quote_currency: str

    units: Decimal
    notional_quote: Decimal
    notional_eur: Decimal

    pip: Decimal
    #: EUR gained or lost per pip **by the whole position**.
    pip_value_eur: Decimal
    stop_distance_pips: Decimal
    stop_distance_quote: Decimal
    entry_price: Decimal
    stop_price: Decimal

    #: The budget: capital x risk%.
    planned_risk_eur: Decimal
    #: What the floored position actually risks. Never more than the budget.
    risk_eur: Decimal

    margin_eur: Decimal
    max_leverage: int
    #: Why ``max_leverage`` is what it is, in words, travelling with the number.
    leverage_basis: str = ESMA_ASSUMPTION
    #: How many quote-currency units one euro bought when this was sized.
    eur_quote_rate: Decimal


class ForexSizingOutcome(Frozen):
    """A sized position, or a named reason there is none.

    ``ideal_units`` survives a rejection on purpose. §7.2 expects
    ``BELOW_MIN_TICKET`` to fire often at EUR 200, and "how often, and by how much"
    is a measurement the first window exists to produce — which it cannot if the
    number is discarded with the rejection.
    """

    symbol: str
    sizing: ForexSizing | None = None
    rejection: ForexRejection | None = None
    detail: str = ""
    ideal_units: Decimal | None = None
    min_trade_size: Decimal | None = None

    @property
    def approved(self) -> bool:
        return self.sizing is not None


def risk_budget_eur(capital_eur: Decimal, risk_per_trade_pct: Decimal) -> Decimal:
    """The euro budget for one trade. The percentage is a percentage — 0.75 is 0.75%."""
    return money(capital_eur * risk_per_trade_pct / HUNDRED)


def pips_between(a: Decimal, b: Decimal, pip: Decimal) -> Decimal:
    """Unsigned distance in pips, using **this instrument's** pip.

    0.0001 on the majors and 0.01 on the yen cross. A shared constant is a 100x error
    on one of the three pairs, with no symptom other than sizes that look plausible.
    """
    if pip <= 0:
        raise ValueError(f"pip must be positive, got {pip}")
    return abs(a - b) / pip


def minimum_viable_capital_eur(
    *,
    stop_distance_pips: Decimal,
    pip: Decimal,
    eur_quote_rate: Decimal,
    min_trade_size: Decimal,
    risk_per_trade_pct: Decimal,
) -> Decimal:
    """The smallest capital at which this stop can be sized at all (§7.2, inverted).

    §7.1 solved for capital instead of units. About EUR 205 on an 18-pip EURUSD stop
    and about EUR 456 on a 40-pip one — which is the arithmetic behind §7.2's warning
    that at EUR 200 only tight-stop setups size correctly, and tight stops are exactly
    where the spread hurts most.

    Very slightly **conservative** by design: it works from an unrounded risk budget,
    where :func:`size_position` quantizes that budget to cents first. So a capital at
    or above this figure always sizes, and one a few cents below it sometimes does
    too. Erring the other way would have this function promise a size the sizer then
    refuses.
    """
    stop_quote = stop_distance_pips * pip
    needed_risk_eur = min_trade_size * stop_quote / eur_quote_rate
    return needed_risk_eur * HUNDRED / risk_per_trade_pct


def size_position(
    *,
    instrument: ForexInstrument,
    entry_price: Decimal,
    stop_price: Decimal,
    capital_eur: Decimal | None,
    risk_per_trade_pct: Decimal,
    eur_quote_rate: Decimal | None,
    max_leverage: int,
) -> ForexSizingOutcome:
    """Size one position, or say why there is none.

    ``entry_price`` and ``stop_price`` are **bid-series levels** (§7.5). The spread is
    a cost, charged once for the round trip in :mod:`sentinel.fx.costs`, not a shift
    applied to these.
    """
    if capital_eur is None or capital_eur <= 0:
        return ForexSizingOutcome(
            symbol=instrument.symbol,
            rejection=ForexRejection.NO_CAPITAL,
            detail="no capital is set, so nothing can be sized",
        )
    if eur_quote_rate is None or eur_quote_rate <= 0:
        return ForexSizingOutcome(
            symbol=instrument.symbol,
            rejection=ForexRejection.FX_RATE_UNAVAILABLE,
            detail=(
                f"no EUR->{instrument.quote_currency} rate — sizing in euro is "
                f"impossible and is not guessed at"
            ),
        )

    stop_distance_quote = abs(entry_price - stop_price)
    if stop_distance_quote <= 0:
        raise ValueError(
            f"{instrument.symbol}: stop distance is zero — entry {entry_price} and stop "
            f"{stop_price} are the same price. Coherence belongs to the gate; sizing "
            f"needs a positive distance."
        )

    planned_risk_eur = risk_budget_eur(capital_eur, risk_per_trade_pct)
    ideal_units = floor_to_decimals(
        planned_risk_eur * eur_quote_rate / stop_distance_quote, instrument.amount_decimals
    )

    if ideal_units < instrument.min_trade_size:
        return ForexSizingOutcome(
            symbol=instrument.symbol,
            rejection=ForexRejection.BELOW_MIN_TICKET,
            detail=(
                f"the intended risk sizes to {ideal_units} units, below the venue "
                f"minimum of {instrument.min_trade_size}. Rounding up would take more "
                f"risk than the budget allows, so it is not done."
            ),
            ideal_units=ideal_units,
            min_trade_size=instrument.min_trade_size,
        )

    notional_quote = money(ideal_units * entry_price)
    notional_eur = money(ideal_units * entry_price / eur_quote_rate)
    # Derived from the *stored* notional, so a reader can reproduce it from the two
    # numbers in front of them — the same convention that makes net RR reconcile by
    # hand on a crypto card.
    margin_eur = money(notional_eur / max_leverage)

    return ForexSizingOutcome(
        symbol=instrument.symbol,
        ideal_units=ideal_units,
        min_trade_size=instrument.min_trade_size,
        sizing=ForexSizing(
            symbol=instrument.symbol,
            quote_currency=instrument.quote_currency,
            units=ideal_units,
            notional_quote=notional_quote,
            notional_eur=notional_eur,
            pip=instrument.pip,
            pip_value_eur=pip_value(ideal_units * instrument.pip / eur_quote_rate),
            stop_distance_pips=pips_between(entry_price, stop_price, instrument.pip),
            stop_distance_quote=stop_distance_quote,
            entry_price=entry_price,
            stop_price=stop_price,
            planned_risk_eur=planned_risk_eur,
            risk_eur=money(ideal_units * stop_distance_quote / eur_quote_rate),
            margin_eur=margin_eur,
            max_leverage=max_leverage,
            eur_quote_rate=eur_quote_rate,
        ),
    )


__all__ = [
    "ESMA_ASSUMPTION",
    "ForexSizing",
    "ForexSizingOutcome",
    "minimum_viable_capital_eur",
    "pips_between",
    "risk_budget_eur",
    "size_position",
]
