"""The reserved crypto budget floor — FOREX.md §13 decision 6, M10a open question R5.

The sub-budgets sum to 14 under an 11 ceiling on purpose, so the markets compete for
the last dollar. That is right between two *measured* markets and wrong the day an
unmeasured market joins a measured one: crypto has a live measurement window running,
and a forex-heavy morning could take the shared dollar out from under it.

**The load-bearing test in this file is
``test_crypto_verdicts_are_identical_with_the_floor_shipped_and_forex_disabled``.**
This is a change to a live spend rail, made while that rail is running, so the first
thing it has to prove is that it changes nothing today.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from sentinel.core.config import AppConfig, LLMConfig, MarketConfig, load_config
from sentinel.core.markets import Market
from sentinel.llm.spend import (
    SpendScope,
    SpendState,
    SpendTotals,
    evaluate_market_spend,
    reserved_elsewhere_usd,
)
from tests.test_config import LEGACY_DEPLOYED

LLM = LLMConfig()
CRYPTO = MarketConfig(
    llm_daily_budget_usd=Decimal(10),
    llm_daily_warn_usd=Decimal(7),
    llm_reserved_floor_usd=Decimal(8),
)
FOREX = MarketConfig(llm_daily_budget_usd=Decimal(4), llm_daily_warn_usd=Decimal(3))
CEILING = Decimal(11)


def totals(day: str) -> SpendTotals:
    return SpendTotals(day_usd=Decimal(day), month_usd=Decimal(day), calls=1)


def verdict(
    *, market: MarketConfig, mine: str, everyone: str, held: str = "0"
) -> tuple[SpendState, SpendScope]:
    result = evaluate_market_spend(
        market_totals=totals(mine),
        global_totals=totals(everyone),
        market=market,
        global_limit_usd=CEILING,
        config=LLM,
        reserved_elsewhere=Decimal(held),
    )
    return result.state, result.scope


# ── what has to be true today ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spent", "expected"),
    [
        ("0", (SpendState.OK, SpendScope.MARKET)),
        ("6.99", (SpendState.OK, SpendScope.MARKET)),
        ("7", (SpendState.WARN, SpendScope.MARKET)),
        ("9.99", (SpendState.WARN, SpendScope.MARKET)),
        ("10", (SpendState.LIMIT_REACHED, SpendScope.MARKET)),
        ("11", (SpendState.LIMIT_REACHED, SpendScope.GLOBAL)),
    ],
)
def test_crypto_verdicts_are_identical_with_the_floor_shipped_and_forex_disabled(
    spent: str, expected: tuple[SpendState, SpendScope]
) -> None:
    """The whole point. With forex disabled it spends nothing and reserves nothing, so
    crypto's effective ceiling is the full 11 and every verdict is what it was before
    this milestone existed. Crypto is mid-measurement; it must not notice this change.
    """
    config = load_config()
    held = reserved_elsewhere_usd(
        for_market=Market.CRYPTO,
        markets={name: config.market(name) for name in config.enabled_markets},
        day_spend_by_market={Market.CRYPTO: Decimal(spent)},
    )
    assert held == Decimal(0)
    assert verdict(market=CRYPTO, mine=spent, everyone=spent, held=str(held)) == expected


def test_a_markets_own_floor_never_reserves_anything_against_itself() -> None:
    """Crypto's floor protects crypto *from other markets*. Counting it against crypto
    would be the rail eating the thing it exists to feed."""
    assert reserved_elsewhere_usd(
        for_market=Market.CRYPTO,
        markets={Market.CRYPTO: CRYPTO, Market.FOREX: FOREX},
        day_spend_by_market={},
    ) == Decimal(0)


def test_a_disabled_market_reserves_nothing() -> None:
    """It cannot spend, so holding money for it would starve a live market for a dollar
    nobody can use. The caller passes enabled markets only, and this pins why."""
    config = load_config()
    assert Market.FOREX not in config.enabled_markets
    assert config.market(Market.FOREX).llm_reserved_floor_usd == Decimal(0)


# ── what the floor does once forex can spend ───────────────────────────────


def test_the_floor_stops_forex_taking_the_dollar_crypto_is_reserved() -> None:
    """Crypto has spent 2 of its 8, so 6 is still held and forex's effective ceiling is
    5. At a global 5 forex stops — under its own 4 budget having spent 3, which is
    exactly the case the competing sub-budgets would have let through."""
    held = reserved_elsewhere_usd(
        for_market=Market.FOREX,
        markets={Market.CRYPTO: CRYPTO, Market.FOREX: FOREX},
        day_spend_by_market={Market.CRYPTO: Decimal(2), Market.FOREX: Decimal(3)},
    )
    assert held == Decimal(6)
    assert verdict(market=FOREX, mine="3", everyone="5", held="6") == (
        SpendState.LIMIT_REACHED,
        SpendScope.RESERVED,
    )


def test_only_the_unspent_part_of_a_floor_is_held() -> None:
    """A market that has spent its floor is no longer protecting anything, and keeping
    the money reserved would shrink everyone else's ceiling for no benefit."""
    spent_it_all = reserved_elsewhere_usd(
        for_market=Market.FOREX,
        markets={Market.CRYPTO: CRYPTO, Market.FOREX: FOREX},
        day_spend_by_market={Market.CRYPTO: Decimal(8)},
    )
    assert spent_it_all == Decimal(0)
    # And overspending its floor never turns into a negative reservation.
    assert reserved_elsewhere_usd(
        for_market=Market.FOREX,
        markets={Market.CRYPTO: CRYPTO, Market.FOREX: FOREX},
        day_spend_by_market={Market.CRYPTO: Decimal(10)},
    ) == Decimal(0)


def test_the_reserved_rail_is_reported_as_its_own_scope() -> None:
    """Three different causes call for three different actions, and two of them read
    almost identically on a status card. "Forex hit its own budget" is answered by
    raising forex's number; "forex was held back for crypto" is answered by deciding
    whether the floor is still the right call. Naming the wrong one sends the owner
    at the wrong lever."""
    assert SpendScope.RESERVED.value == "reserved"
    assert verdict(market=FOREX, mine="1", everyone="5", held="6")[1] is SpendScope.RESERVED
    assert verdict(market=FOREX, mine="4", everyone="4", held="0")[1] is SpendScope.MARKET
    assert verdict(market=FOREX, mine="1", everyone="11", held="6")[1] is SpendScope.GLOBAL


def test_the_hard_ceiling_still_wins_over_the_reserved_rail() -> None:
    """A misconfiguration must fail safe. The global ceiling is checked first, so no
    arrangement of floors can ever mask it."""
    assert verdict(market=FOREX, mine="0", everyone="11", held="0") == (
        SpendState.LIMIT_REACHED,
        SpendScope.GLOBAL,
    )


def test_a_zero_floor_leaves_the_function_exactly_as_it_was() -> None:
    """The default. Every existing caller that does not pass ``reserved_elsewhere``
    gets pre-M10b-2 behaviour, which is what makes this safe to land mid-window."""
    plain = evaluate_market_spend(
        market_totals=totals("5"),
        global_totals=totals("5"),
        market=CRYPTO,
        global_limit_usd=CEILING,
        config=LLM,
    )
    assert (plain.state, plain.scope) == (SpendState.OK, SpendScope.MARKET)


# ── the config rails ───────────────────────────────────────────────────────


def test_a_floor_above_its_own_market_budget_fails_at_config_load() -> None:
    """It would reserve money that market is not permitted to spend — starving every
    other market to hold a dollar nobody can use."""
    with pytest.raises(ValidationError, match="exceeds its own daily budget"):
        AppConfig(
            markets={
                Market.CRYPTO: MarketConfig(
                    llm_daily_budget_usd=Decimal(4), llm_reserved_floor_usd=Decimal(9)
                )
            }
        )


def test_floors_summing_past_the_global_ceiling_fail_at_config_load() -> None:
    """A deployment that could never honour its own promises."""
    with pytest.raises(ValidationError, match="above the global ceiling"):
        AppConfig(
            llm_daily_budget_global_usd=Decimal(11),
            markets={
                Market.CRYPTO: MarketConfig(
                    llm_daily_budget_usd=Decimal(10), llm_reserved_floor_usd=Decimal(8)
                ),
                Market.FOREX: MarketConfig(
                    llm_daily_budget_usd=Decimal(6), llm_reserved_floor_usd=Decimal(6)
                ),
            },
        )


def test_the_shipped_config_carries_the_floor_the_owner_decided() -> None:
    config = load_config()
    assert config.market(Market.CRYPTO).llm_reserved_floor_usd == Decimal(8)
    # `8`, never `8.00` — YAML would parse that as a float and render "$8.0".
    assert str(config.market(Market.CRYPTO).llm_reserved_floor_usd) == "8"


def test_the_legacy_deployed_config_behaves_identically() -> None:
    """The frozen pre-M10a config has no floor at all.

    **Correction (2026-08-21, hygiene session):** this docstring used to say this was
    "the config the server actually runs", because ``docs/DEPLOY.md`` §6/§13 edit
    ``config.yaml`` in place. Neither half is true — the live server's file is
    byte-identical to the committed one and carries the floor, and §6/§13 never edited
    anything in place because the config is baked into the image. What is asserted
    below is unchanged and still worth asserting: a legacy-shaped file, which this
    fixture is and which any un-migrated file would be, does not carry
    ``llm_reserved_floor_usd``. That is the one difference ``tests/test_config.py``
    names — and it is behaviourally invisible,
    because a floor only ever reserves against *other* markets and a legacy config has
    exactly one. Proved here rather than assumed, since the alternative is a rail that
    silently does not apply where it was meant to.
    """
    legacy = load_config(LEGACY_DEPLOYED)
    for spent in ("0", "7", "10", "11"):
        held = reserved_elsewhere_usd(
            for_market=Market.CRYPTO,
            markets={name: legacy.market(name) for name in legacy.enabled_markets},
            day_spend_by_market={Market.CRYPTO: Decimal(spent)},
        )
        assert held == Decimal(0)
        assert verdict(
            market=legacy.market(Market.CRYPTO), mine=spent, everyone=spent, held=str(held)
        ) == verdict(market=CRYPTO, mine=spent, everyone=spent, held="0")
