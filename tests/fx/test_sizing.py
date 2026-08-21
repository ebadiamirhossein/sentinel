"""FX position sizing — docs/specs/FOREX.md §7.1 and §7.2.

**Written before the module, from hand-computed fixtures**, exactly as M4 built the
crypto risk engine. The arithmetic below is worked by hand in the M10b report and
every figure that also appears in the spec is cross-checked against it:

* §7.2's table at EUR 200 and 0.75% risk — 974 units on an 18-pip stop, 701 on 25,
  438 on 40 — is reproduced exactly, which is the strongest available evidence that
  this formula is the one the spec was describing.
* §7.2's inversion — "about EUR 205 at an 18-pip stop, about EUR 456 at 40 pips" —
  is reproduced from the other direction.

USDJPY is not a variation on the other two. Its pip is 0.01 rather than 0.0001 and
its EUR conversion goes through **EURJPY**, not EURUSD, so a single wrong constant
there is a 100x error in the pip and a ~145x error in the rate. It gets its own
fixtures throughout.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.models import ForexRejection
from sentinel.fx.sizing import (
    ForexSizingOutcome,
    minimum_viable_capital_eur,
    pips_between,
    risk_budget_eur,
    size_position,
)

NOW = datetime(2026, 8, 21, 7, 0, tzinfo=UTC)

#: Frankfurter, base=EUR. journal/M10b_SPIKE.md quotes EURUSD ~1.169 live.
EUR_USD = Decimal("1.169")
#: **EURJPY, not EURUSD.** §7.1: USDJPY is quoted in JPY, so the euro conversion is
#: how many yen one euro buys. Frankfurter already serves it — no new dependency.
EUR_JPY = Decimal("170")

CAPITAL = Decimal("200")
RISK_PCT = Decimal("0.75")
MAX_LEVERAGE = 30


def instrument(symbol: str, *, decimals: int, tick: str) -> ForexInstrument:
    return ForexInstrument(
        symbol=symbol,
        uic={"EURUSD": 21, "GBPUSD": 31, "USDJPY": 42}[symbol],
        decimals=decimals,
        pip=Decimal(1).scaleb(-decimals),
        tick_size=Decimal(tick),
        min_trade_size=Decimal("1000"),
        amount_decimals=2,
        base_currency=symbol[:3],
        quote_currency=symbol[3:],
        resolved_at=NOW,
    )


EURUSD = instrument("EURUSD", decimals=4, tick="0.00001")
GBPUSD = instrument("GBPUSD", decimals=4, tick="0.00001")
USDJPY = instrument("USDJPY", decimals=2, tick="0.001")


def size(
    inst: ForexInstrument,
    *,
    entry: str,
    stop: str,
    capital: Decimal = CAPITAL,
    rate: Decimal | None = None,
    risk_pct: Decimal = RISK_PCT,
) -> ForexSizingOutcome:
    return size_position(
        instrument=inst,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        capital_eur=capital,
        risk_per_trade_pct=risk_pct,
        eur_quote_rate=rate if rate is not None else (EUR_JPY if inst is USDJPY else EUR_USD),
        max_leverage=MAX_LEVERAGE,
    )


# ── the risk budget ─────────────────────────────────────────────────────────


def test_the_risk_budget_is_a_percentage_of_capital() -> None:
    assert risk_budget_eur(Decimal("200"), Decimal("0.75")) == Decimal("1.50")
    assert risk_budget_eur(Decimal("10000"), Decimal("0.75")) == Decimal("75.00")


def test_a_distance_in_pips_uses_the_instruments_own_pip() -> None:
    """0.0001 on the majors and 0.01 on the yen cross. A shared constant here is a
    100x error on one of the three pairs, with no symptom but wrong sizes."""
    assert pips_between(Decimal("1.1692"), Decimal("1.1674"), EURUSD.pip) == Decimal("18")
    assert pips_between(Decimal("148.50"), Decimal("148.32"), USDJPY.pip) == Decimal("18")
    # Direction does not change a distance.
    assert pips_between(Decimal("1.1674"), Decimal("1.1692"), EURUSD.pip) == Decimal("18")


# ── §7.2's table, reproduced exactly ────────────────────────────────────────


@pytest.mark.parametrize(
    ("stop", "expected_units"),
    [
        ("1.1674", Decimal("974.16")),  # 18 pips — §7.2 says 974
        ("1.1667", Decimal("701.40")),  # 25 pips — §7.2 says 701
        ("1.1652", Decimal("438.37")),  # 40 pips — §7.2 says 438
    ],
    ids=["18-pip", "25-pip", "40-pip"],
)
def test_the_spec_sizing_table_at_two_hundred_euro(stop: str, expected_units: Decimal) -> None:
    """units = risk_eur x rate(EUR->quote) / stop_distance_in_quote.

    1.50 x 1.169 / 0.0018 = 974.1666..., floored to the venue's two decimals.
    """
    outcome = size(EURUSD, entry="1.1692", stop=stop)
    assert outcome.ideal_units == expected_units
    assert outcome.rejection is ForexRejection.BELOW_MIN_TICKET
    assert outcome.sizing is None


def test_a_wider_stop_needs_a_smaller_position_which_makes_this_worse() -> None:
    """§7.2's counter-intuitive point, asserted rather than described."""
    tight = size(EURUSD, entry="1.1692", stop="1.1674").ideal_units
    wide = size(EURUSD, entry="1.1692", stop="1.1652").ideal_units
    assert tight is not None and wide is not None
    assert wide < tight


# ── the minimum ticket (§7.2) ───────────────────────────────────────────────


def test_below_the_minimum_ticket_is_a_rejection_that_carries_how_far_below() -> None:
    """How often this fires at EUR 200 is itself a measurement worth having, so the
    ideal size survives the rejection instead of being thrown away with it."""
    outcome = size(EURUSD, entry="1.1692", stop="1.1674")
    assert outcome.rejection is ForexRejection.BELOW_MIN_TICKET
    assert outcome.ideal_units == Decimal("974.16")
    assert outcome.min_trade_size == Decimal("1000")
    assert "1000" in outcome.detail


def test_the_minimum_is_never_met_by_rounding_up_into_more_risk() -> None:
    """§7.2, and the rule under it: under-risking is fine, over-risking is not."""
    outcome = size(EURUSD, entry="1.1692", stop="1.1674")
    assert outcome.sizing is None, "974 units must not become 1000 units of risk"


@pytest.mark.parametrize(
    ("capital", "clears"),
    [("204", False), ("205", True)],
    ids=["204-just-below", "205-just-above"],
)
def test_the_minimum_viable_capital_at_an_eighteen_pip_stop(capital: str, clears: bool) -> None:
    """§7.2's inversion: "about EUR 205", and EUR 205 is exactly where it starts.

    The boundary sits between 204 and 205 rather than between 205 and 206 because the
    risk budget quantizes to cents before it sizes anything: at EUR 205 the budget is
    1.5375, which stores as 1.54, and that half-cent is worth ~1.5 units. Crypto's
    ``risk_budget_eur`` has quantized the same way since M4 and the figure on the card
    has to be the figure that was sized against, so this matches rather than invents a
    second convention. Recorded because it is exactly the sort of half-cent that makes
    a hand-computed fixture disagree with an implementation for a real reason.
    """
    outcome = size(EURUSD, entry="1.1692", stop="1.1674", capital=Decimal(capital))
    assert (outcome.sizing is not None) is clears


@pytest.mark.parametrize(
    ("capital", "clears"),
    [("456", False), ("457", True)],
    ids=["456-just-below", "457-just-above"],
)
def test_the_minimum_viable_capital_at_a_forty_pip_stop(capital: str, clears: bool) -> None:
    """§7.2's other inversion: "about EUR 456"."""
    outcome = size(EURUSD, entry="1.1692", stop="1.1652", capital=Decimal(capital))
    assert (outcome.sizing is not None) is clears


def test_minimum_viable_capital_is_computable_directly() -> None:
    """The same two numbers from the other direction, so the report can quote them
    without re-deriving them by hand."""
    at_18 = minimum_viable_capital_eur(
        stop_distance_pips=Decimal("18"),
        pip=EURUSD.pip,
        eur_quote_rate=EUR_USD,
        min_trade_size=EURUSD.min_trade_size,
        risk_per_trade_pct=RISK_PCT,
    )
    at_40 = minimum_viable_capital_eur(
        stop_distance_pips=Decimal("40"),
        pip=EURUSD.pip,
        eur_quote_rate=EUR_USD,
        min_trade_size=EURUSD.min_trade_size,
        risk_per_trade_pct=RISK_PCT,
    )
    assert Decimal("205") < at_18 <= Decimal("206")
    assert Decimal("456") < at_40 <= Decimal("457")


# ── the three pairs, sized in full ──────────────────────────────────────────


def test_eurusd_sized_at_three_hundred_euro() -> None:
    """Hand-computed. risk 2.25, rate 1.169, stop 0.0018:

    units        = 2.25 x 1.169 / 0.0018 = 1461.25
    notional USD = 1461.25 x 1.1692      = 1708.4935
    notional EUR = 1708.4935 / 1.169     = 1461.50
    pip value    = 1461.25 x 0.0001 / 1.169 = 0.125 EUR per pip
    risk         = 0.125 x 18            = 2.25 EUR  (the budget, exactly)
    margin       = 1461.50 / 30          = 48.72
    """
    outcome = size(EURUSD, entry="1.1692", stop="1.1674", capital=Decimal("300"))
    plan = outcome.sizing
    assert plan is not None
    assert plan.units == Decimal("1461.25")
    assert plan.notional_quote == Decimal("1708.49")
    assert plan.notional_eur == Decimal("1461.50")
    assert plan.pip_value_eur == Decimal("0.125")
    assert plan.stop_distance_pips == Decimal("18")
    assert plan.planned_risk_eur == Decimal("2.25")
    assert plan.risk_eur == Decimal("2.25")
    assert plan.margin_eur == Decimal("48.72")
    assert plan.quote_currency == "USD"
    assert plan.eur_quote_rate == EUR_USD


def test_gbpusd_sized_at_three_hundred_euro() -> None:
    """Same quote currency and same pip as EURUSD, so the **unit count is identical**
    — units depend on risk, rate and stop distance, never on the instrument's price.
    The notional differs because the price does.

        notional USD = 1461.25 x 1.3450 = 1965.38125
        notional EUR = 1965.38125 / 1.169 = 1681.25
        margin       = 1681.25 / 30       = 56.04
    """
    outcome = size(GBPUSD, entry="1.3450", stop="1.3432", capital=Decimal("300"))
    plan = outcome.sizing
    assert plan is not None
    assert plan.units == Decimal("1461.25")
    assert plan.notional_quote == Decimal("1965.38")
    assert plan.notional_eur == Decimal("1681.25")
    assert plan.margin_eur == Decimal("56.04")
    assert plan.risk_eur == Decimal("2.25")


def test_usdjpy_uses_eurjpy_and_a_one_hundredth_pip() -> None:
    """The fixture the DoD names, and the one where a shared constant would be a
    100x error in the pip and a ~145x error in the rate.

        stop         = 18 x 0.01            = 0.18 JPY
        units        = 1.50 x 170 / 0.18    = 1416.666..., floored to 1416.66
        notional JPY = 1416.66 x 148.50     = 210374.01
        notional EUR = 210374.01 / 170      = 1237.49
        risk         = 1416.66 x 0.18 / 170 = 1.4999929 -> 1.50
        margin       = 1237.49 / 30         = 41.25

    And note what this shows: at EUR 200, USDJPY **clears** the 1000-unit minimum on
    the same 18-pip stop where EURUSD does not, because a yen pip buys far more units
    per euro of risk.
    """
    outcome = size(USDJPY, entry="148.50", stop="148.32")
    plan = outcome.sizing
    assert plan is not None, "USDJPY clears the minimum at EUR 200 where EURUSD does not"
    assert plan.pip == Decimal("0.01")
    assert plan.eur_quote_rate == EUR_JPY
    assert plan.quote_currency == "JPY"
    assert plan.units == Decimal("1416.66")
    assert plan.stop_distance_quote == Decimal("0.18")
    assert plan.notional_quote == Decimal("210374.01")
    assert plan.notional_eur == Decimal("1237.49")
    assert plan.risk_eur == Decimal("1.50")
    assert plan.margin_eur == Decimal("41.25")


def test_using_eurusd_for_usdjpy_would_be_visibly_wrong() -> None:
    """The mistake this fixture exists to catch, made deliberately once."""
    right = size(USDJPY, entry="148.50", stop="148.32").sizing
    wrong = size(USDJPY, entry="148.50", stop="148.32", rate=EUR_USD).sizing
    assert right is not None
    assert wrong is None, "at EUR 200 the wrong rate sizes to ~10 units, far below the minimum"


# ── risk never exceeds the budget ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("inst", "entry", "stop", "capital"),
    [
        (EURUSD, "1.1692", "1.1674", "300"),
        (GBPUSD, "1.3450", "1.3432", "300"),
        (USDJPY, "148.50", "148.32", "200"),
        (EURUSD, "1.1692", "1.1652", "1000"),
    ],
)
def test_realized_risk_never_exceeds_the_planned_budget(
    inst: ForexInstrument, entry: str, stop: str, capital: str
) -> None:
    """Units floor, so the error can only ever be conservative."""
    outcome = size(inst, entry=entry, stop=stop, capital=Decimal(capital))
    plan = outcome.sizing
    assert plan is not None
    assert plan.risk_eur <= plan.planned_risk_eur


def test_units_are_floored_to_the_venue_granularity_never_rounded_up() -> None:
    outcome = size(USDJPY, entry="148.50", stop="148.32")
    plan = outcome.sizing
    assert plan is not None
    assert plan.units == Decimal("1416.66"), "1416.6666... floors, it does not round to .67"


# ── margin is a documented assumption, and says so (§7.6) ───────────────────


def test_leverage_is_labelled_a_config_assumption_not_a_venue_figure() -> None:
    """D-f: instrument details carry no ``MarginRates`` and no ``MarginTiers``, so
    the ESMA 30:1 cap is an assumption. §7.6 requires it to be labelled wherever it
    surfaces, and a label nobody can drop is one carried by the value itself."""
    plan = size(EURUSD, entry="1.1692", stop="1.1674", capital=Decimal("300")).sizing
    assert plan is not None
    assert plan.max_leverage == 30
    assert "assum" in plan.leverage_basis.lower()
    assert "ESMA" in plan.leverage_basis


def test_there_is_nowhere_to_put_a_liquidation_buffer_or_a_funding_cost() -> None:
    """§7.6, and the owner's ruling of 2026-08-21.

    CFD margin is account-level: there is no per-position liquidation price, so the
    crypto liquidation-buffer rule does not apply and "must not be faked". The
    stronger form of that is what is asserted here — not that the fields are empty,
    but that they do not exist. A meaningless value that looks meaningful is the
    exact failure class this project keeps meeting.
    """
    plan = size(EURUSD, entry="1.1692", stop="1.1674", capital=Decimal("300")).sizing
    assert plan is not None
    fields = set(type(plan).model_fields)
    for forbidden in ("liq_distance_pct", "liq_buffer_ok", "liquidation_price", "funding_rate"):
        assert forbidden not in fields
    assert not [name for name in fields if "liq" in name or "funding" in name]


# ── the inputs that make sizing impossible ──────────────────────────────────


def test_no_capital_is_a_named_rejection() -> None:
    outcome = size_position(
        instrument=EURUSD,
        entry_price=Decimal("1.1692"),
        stop_price=Decimal("1.1674"),
        capital_eur=None,
        risk_per_trade_pct=RISK_PCT,
        eur_quote_rate=EUR_USD,
        max_leverage=MAX_LEVERAGE,
    )
    assert outcome.rejection is ForexRejection.NO_CAPITAL
    assert outcome.sizing is None


@pytest.mark.parametrize("rate", [None, Decimal("0")], ids=["missing", "zero"])
def test_a_missing_conversion_rate_stops_sizing_rather_than_guessing(
    rate: Decimal | None,
) -> None:
    """§12: Frankfurter unavailable and too stale to reuse means no sizing, so no
    signal. It never means "assume parity"."""
    outcome = size_position(
        instrument=EURUSD,
        entry_price=Decimal("1.1692"),
        stop_price=Decimal("1.1674"),
        capital_eur=CAPITAL,
        risk_per_trade_pct=RISK_PCT,
        eur_quote_rate=rate,
        max_leverage=MAX_LEVERAGE,
    )
    assert outcome.rejection is ForexRejection.FX_RATE_UNAVAILABLE
    assert outcome.sizing is None


def test_a_stop_on_top_of_the_entry_is_a_programming_error_not_a_rejection() -> None:
    """Coherence — direction, stop side, ATR bounds — belongs to a gate, which is
    M10c. What sizing needs is a positive distance, and a zero one is a bug upstream."""
    with pytest.raises(ValueError, match="stop distance"):
        size(EURUSD, entry="1.1692", stop="1.1692")
