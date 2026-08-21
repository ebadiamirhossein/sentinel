"""Turning a stored ``signals.plan`` row back into the plan it was (FOREX.md §16.7).

One function, and its whole design is a refusal.

``SignalRecord.plan`` is ``TradePlan | ForexPlan``, and :class:`ForexPlan` deliberately
**mirrors** ``TradePlan``'s shared vocabulary — ``symbol``, ``entries``, ``avg_entry``,
``stop``, ``targets``, ``expires_at``, ``risk_eur`` — because that is what lets one
signals table, one publisher and one tracker serve both markets with no translation
layer. The mirroring is the feature and it is also the hazard: two models that look
alike are two models a wrong guess could confuse, and a wrong guess here would not raise.
It would produce a *plausible object* — the pip derivation's shape exactly, where
``10 ** -(decimals - 1)`` gives believable prices and no error.

So this **dispatches on the row's market**, and the obvious convenience — try one model,
fall back to the other — is precisely what is forbidden. A fallback is the mechanism
that turns a mis-dispatch into a plausible object: it takes the case where the code was
wrong about which market it was reading and makes it look like success.

What actually makes the dispatch safe is not this function but the shape of the two
models, and that is pinned separately: both are ``extra="forbid"`` with required fields
the other lacks, so each genuinely refuses the other's payload, and
``tests/bot/test_plan_dispatch.py`` asserts that **neither model's field set is a subset
of the other's** — the property the refusal rests on, so a future field addition cannot
quietly make one plan validate as the other.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.fx.plan import ForexPlan
from sentinel.risk.models import TradePlan

AnyPlan = TradePlan | ForexPlan

#: Market -> the model its plans are stored as. A mapping rather than an ``if``, so
#: adding a market without deciding this is a ``KeyError`` at the seam rather than a
#: silent fall-through to whichever branch happened to be last.
PLAN_MODELS: dict[Market, type[AnyPlan]] = {
    Market.CRYPTO: TradePlan,
    Market.FOREX: ForexPlan,
}


def plan_model_for(market: Market) -> type[AnyPlan]:
    """Which model this market's plans are stored as."""
    try:
        return PLAN_MODELS[market]
    except KeyError:
        raise KeyError(
            f"no plan model registered for market {market.value!r}. A new market must "
            f"name its plan model here; guessing one would be FOREX.md §16.7's "
            f"forbidden fallback with extra steps."
        ) from None


def market_of(row: Any) -> Market:
    """The market a stored row belongs to, read from its own column.

    ``signals.market`` is ``NOT NULL`` with a ``'crypto'`` server default (migration
    ``0010``), so a ``None`` here is never a database row — it is one built in memory
    that never went through an insert. ``LEGACY_MARKET`` is both the value the migration
    backfilled and the value the column would default to, so reading it that way agrees
    with what Postgres would have stored rather than guessing something else.
    """
    value = getattr(row, "market", None)
    if value is None:
        return LEGACY_MARKET
    return value if isinstance(value, Market) else Market(value)


def plan_of(payload: Any, market: Market) -> AnyPlan:
    """Rehydrate a stored ``signals.plan`` JSONB payload for ``market``.

    Raises rather than falling back. A row whose market and whose payload disagree is a
    real defect — a mis-stamped write, or a market column that drifted — and the useful
    outcome is a loud failure on that one signal, not a plan built from the wrong model
    that every downstream number is then computed against.
    """
    return plan_model_for(market).model_validate(payload)


class Rung(Protocol):
    """The rung vocabulary both markets share (FOREX.md §16.2).

    Exists so the tracker's detection path can iterate ``plan.entries`` without
    knowing which market it is in. Without it, ``tuple[EntryRung, ...] |
    tuple[ForexEntryRung, ...]`` joins to ``BaseModel`` under ``mypy --strict`` and
    every attribute read becomes an error — which is the type checker correctly
    pointing out that the union has no *stated* common shape. This states it.

    ``qty`` is here rather than ``units`` for exactly this reason: see
    :class:`sentinel.fx.plan.ForexEntryRung`, where the reversal is recorded.
    """

    @property
    def price(self) -> Decimal: ...
    @property
    def weight_pct(self) -> Decimal: ...
    @property
    def qty(self) -> Decimal: ...
    @property
    def notional_eur(self) -> Decimal: ...
    @property
    def distance_pct(self) -> Decimal: ...


def rungs_of(plan: AnyPlan) -> tuple[Rung, ...]:
    """``plan.entries`` as the shared shape. A view, not a conversion."""
    return tuple(plan.entries)


def eur_quote_rate_of(plan: AnyPlan) -> Decimal:
    """How many **quote-currency units one euro buys**, whichever market this is.

    The two markets spell one concept differently and both spellings are right.
    Crypto's ``eurusd_rate`` is USDT per EUR, and USDT is its quote currency. Forex's
    ``eur_quote_rate`` is EURUSD for the majors and **EURJPY** for USDJPY (§7.1) —
    which is precisely why it cannot be called ``eurusd_rate`` there: on the yen cross
    that name would be a fabricated label on a correct number, and reaching for EURUSD
    instead is a ~145x error that sizes a position to nothing and looks unremarkable.

    So the reconciliation lives here, in one function with the reason attached, rather
    than as an ``isinstance`` in each of the four places the tracker converts to euro.
    """
    if isinstance(plan, ForexPlan):
        return plan.eur_quote_rate
    return plan.eurusd_rate


def leverage_of(plan: AnyPlan) -> str:
    """The leverage figure a surface shows, as text, with its meaning attached.

    The two markets' numbers are **not** the same kind of thing and must not read as
    though they were. Crypto's ``suggested_leverage`` is *derived* — solved for so the
    liquidation price stays clear of the stop by the configured multiple. Forex has no
    per-position liquidation price to derive one from (§7.6), so ``max_leverage`` is a
    configured **cap** resting on an ESMA assumption, and calling it a suggestion would
    turn a documented assumption into a recommendation.

    Returned as a string because it is a display value and the difference between the
    two is a word, not a number. ``PositionView`` was already all-``str`` for that
    reason.
    """
    if isinstance(plan, ForexPlan):
        return f"{plan.max_leverage}x max"
    return str(plan.suggested_leverage)


def journal_leverage_of(plan: AnyPlan) -> int | None:
    """The **per-position** leverage a journal row records, or ``None`` where there
    is none — which is forex, always (M10d).

    Not :func:`leverage_of`. That one returns a display string carrying the word
    ``max``, and the journal's column is a number that a reader sorts and filters. The
    two are different jobs and M10c gave them one function: ``JournalRow.leverage`` is
    ``int | None``, ``str(suggested_leverage)`` coerced quietly for crypto, and
    ``"30x max"`` raised a ``ValidationError`` the first time a forex row reached it —
    which took ``/journal`` down for the **whole workbook**, every population, not just
    the forex rows. Found by composing join 3 (journal/M10d_REPORT.md).

    ``None`` for forex is the honest answer rather than the convenient one. §7.6: CFD
    margin is **account-level**, there is no per-position liquidation price, and the
    30:1 cap is a configured constant identical on every row — so a number here would
    be a per-position figure this market does not have, which is exactly what §2.1 and
    §16.2 group 3 forbid. The cap and its ESMA words still travel on the card, through
    :func:`leverage_of`, where they mean something.
    """
    if isinstance(plan, ForexPlan):
        return None
    return plan.suggested_leverage


def qty_step_of(plan: AnyPlan) -> Decimal:
    """The smallest quantity increment this venue accepts.

    Crypto reads the exchange's ``qty_step`` directly. Saxo publishes an
    ``AmountDecimals`` instead — a precision rather than a step — so the step is
    ``10 ** -decimals``. Same quantity, two ways of stating it, and the conversion
    belongs next to the one above rather than inside the state machine.
    """
    if isinstance(plan, ForexPlan):
        return Decimal(10) ** -plan.instrument.amount_decimals
    return plan.instrument.qty_step


__all__ = [
    "PLAN_MODELS",
    "AnyPlan",
    "Rung",
    "eur_quote_rate_of",
    "journal_leverage_of",
    "leverage_of",
    "market_of",
    "plan_model_for",
    "plan_of",
    "qty_step_of",
    "rungs_of",
]
