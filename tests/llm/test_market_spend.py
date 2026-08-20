"""Two markets, two budgets, one ceiling — and neither can stop the other (Step 4).

The rule M10a exists to make true: **a forex overspend must not stop the crypto
analysis that is being measured**, and once forex is real, the reverse. That is the
whole reason the budgets are per market rather than one number, and it is the test
the milestone brief asks for by name.

The sub-budgets deliberately sum to more than the ceiling ($10 + $4 against $11), so
the markets compete for the last dollar instead of each reserving one a quiet day
would waste. The consequence — that a forex-heavy morning can leave crypto short of
its own $10 — is real, is recorded in journal/M10a_REPORT.md, and is asserted below
rather than left to be discovered.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.core.config import AppConfig, MarketConfig, load_config
from sentinel.core.markets import Market
from sentinel.llm.spend import (
    SpendScope,
    SpendState,
    SpendTotals,
    evaluate_market_spend,
    evaluate_spend,
)


def totals(day: str) -> SpendTotals:
    return SpendTotals(day_usd=Decimal(day), month_usd=Decimal(day), calls=1)


def verdict(
    config: AppConfig, market: Market, *, spent: str, everywhere: str
) -> tuple[SpendState, SpendScope]:
    result = evaluate_market_spend(
        market_totals=totals(spent),
        global_totals=totals(everywhere),
        market=config.market(market),
        global_limit_usd=config.llm_daily_budget_global_usd,
        config=config.llm,
    )
    return result.state, result.scope


@pytest.fixture
def config() -> AppConfig:
    """The shipped config: crypto 10.00, forex 4.00, ceiling 11.00."""
    return load_config()


# --------------------------------------------------------------------------- #
# The headline: markets do not stop each other
# --------------------------------------------------------------------------- #


def test_forex_exhausting_its_budget_does_not_stop_crypto(config: AppConfig) -> None:
    """Forex has spent all $4. Crypto has spent $2 and must carry on.

    The scenario the whole per-market budget exists for. Under one global budget
    this is the cycle where the crypto measurement quietly stops.
    """
    assert verdict(config, Market.FOREX, spent="4.00", everywhere="6.00") == (
        SpendState.LIMIT_REACHED,
        SpendScope.MARKET,
    )
    assert verdict(config, Market.CRYPTO, spent="2.00", everywhere="6.00") == (
        SpendState.OK,
        SpendScope.MARKET,
    )


def test_crypto_exhausting_its_budget_does_not_stop_forex(config: AppConfig) -> None:
    """And the reverse, which matters the day forex is enabled."""
    assert verdict(config, Market.CRYPTO, spent="10.00", everywhere="10.50") == (
        SpendState.LIMIT_REACHED,
        SpendScope.MARKET,
    )
    # Forex is *warned* rather than merely fine: the deployment as a whole is past
    # the global warn level, and saying so is the honest answer. What matters — and
    # what the milestone promises — is that it is not **stopped**.
    # mypy narrows ``state`` past a literal comparison, so "not stopped" is asserted
    # through the verdict's own predicate — which is what the orchestrator branches on.
    result = evaluate_market_spend(
        market_totals=totals("0.50"),
        global_totals=totals("10.50"),
        market=config.market(Market.FOREX),
        global_limit_usd=config.llm_daily_budget_global_usd,
        config=config.llm,
    )
    assert not result.suspends_analysis, "forex must not be stopped by crypto's spend"
    assert result.state is SpendState.WARN
    assert result.scope is SpendScope.GLOBAL


def test_the_global_ceiling_stops_both(config: AppConfig) -> None:
    """$11 spent across the deployment: neither market may buy another analysis,
    however little of it was its own."""
    for market in (Market.CRYPTO, Market.FOREX):
        assert verdict(config, market, spent="0.10", everywhere="11.00") == (
            SpendState.LIMIT_REACHED,
            SpendScope.GLOBAL,
        )


def test_the_ceiling_is_checked_before_a_markets_own_budget(config: AppConfig) -> None:
    """A sub-budget above the ceiling — which the shipped config has, on purpose —
    must never mask it. The same fail-safe ordering ``evaluate_spend`` uses for its
    limit and its warn level."""
    state, scope = verdict(config, Market.CRYPTO, spent="9.00", everywhere="11.50")

    assert (state, scope) == (SpendState.LIMIT_REACHED, SpendScope.GLOBAL)


def test_the_squeeze_is_real_and_asserted(config: AppConfig) -> None:
    """Crypto is stopped at $7 of its own $10 because forex spent $4 first.

    This is the cost of letting the markets compete, and it is a genuine behaviour
    change *the day forex is enabled* — harmless today, since a disabled market
    cannot spend anything. Asserted rather than described, so M10b's decision about
    a reserved floor for the measured market starts from a fact.
    """
    state, scope = verdict(config, Market.CRYPTO, spent="7.00", everywhere="11.00")

    assert (state, scope) == (SpendState.LIMIT_REACHED, SpendScope.GLOBAL)
    assert Decimal("7.00") < config.market(Market.CRYPTO).llm_daily_budget_usd


# --------------------------------------------------------------------------- #
# Warnings, and the single-market equivalence that protects today's behaviour
# --------------------------------------------------------------------------- #


def test_a_market_warns_at_its_own_level(config: AppConfig) -> None:
    assert verdict(config, Market.FOREX, spent="3.00", everywhere="3.00") == (
        SpendState.WARN,
        SpendScope.MARKET,
    )


def test_the_market_warning_is_preferred_when_both_are_crossed(config: AppConfig) -> None:
    """Both warn levels are past. The market's is the more actionable of the two —
    it names the thing whose number the owner would change."""
    state, scope = verdict(config, Market.CRYPTO, spent="8.00", everywhere="9.00")

    assert (state, scope) == (SpendState.WARN, SpendScope.MARKET)


def test_the_global_warning_speaks_when_no_market_has_crossed_its_own(
    config: AppConfig,
) -> None:
    """Two markets each under their own warn level, adding up past the global one.

    Nobody is individually spending too much and the deployment still is — which is
    exactly the case a per-market-only guard would miss.
    """
    state, scope = verdict(config, Market.CRYPTO, spent="5.00", everywhere="8.00")

    assert (state, scope) == (SpendState.WARN, SpendScope.GLOBAL)


@pytest.mark.parametrize("spent", ["0.00", "6.99", "7.00", "9.99", "10.00", "12.00"])
def test_with_one_market_the_verdict_matches_the_pre_m10a_guard(
    config: AppConfig, spent: str
) -> None:
    """The equivalence that makes this safe to deploy mid-measurement.

    With forex disabled, this market's spend *is* the deployment's spend, and the
    shipped crypto budget is the pre-M10a ``llm.daily_spend_limit_usd``. So the
    two-tier guard has to return exactly what the one-tier guard returned — at the
    boundaries as well as in the middle.
    """
    single = evaluate_spend(totals(spent), config.llm)
    two_tier, _ = verdict(config, Market.CRYPTO, spent=spent, everywhere=spent)

    assert two_tier is single


def test_a_lower_market_budget_bites_before_the_old_global_one() -> None:
    """And the guard is not merely ignoring the market budget.

    The sibling of the equivalence test above: if the market's budget were being
    dropped on the floor, every assertion in this file would still pass except this
    one.
    """
    config = load_config()
    lean = config.market(Market.CRYPTO).model_copy(
        update={"llm_daily_budget_usd": Decimal("3"), "llm_daily_warn_usd": Decimal("2")}
    )
    tightened: MarketConfig = lean

    result = evaluate_market_spend(
        market_totals=totals("3.50"),
        global_totals=totals("3.50"),
        market=tightened,
        global_limit_usd=config.llm_daily_budget_global_usd,
        config=config.llm,
    )

    assert result.state is SpendState.LIMIT_REACHED
    assert result.scope is SpendScope.MARKET
    assert evaluate_spend(totals("3.50"), config.llm) is SpendState.OK
