"""Runtime configuration: ARCHITECTURE.md §2's "DB > yaml > defaults", at last wired.

``load_config`` has accepted a ``db_overrides`` mapping since M0 and nothing ever
supplied one, because the DB layer did not exist. These tests cover the merge
itself and the two values that are not ordinary config: ``capital_eur``, which
lives nowhere in ``AppConfig`` at all, and the risk percentage, which lives in
both ``AppConfig`` and ``AccountState``.

**Those two now come from the caller's ``users`` row rather than from
``runtime_settings`` (M8.1)** — they are exactly the values that must not be shared
between people. What is asserted below is unchanged in substance: an unset capital
stays ``None`` so the gate rejects loudly, a set one reaches the gate, and the config
default applies until a user chooses otherwise. Only the source moved.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.bot.runtime import (
    CAPITAL_EUR,
    WATCHLIST,
    Invalid,
    account_state,
    config_overrides,
    effective_config,
    parse_capital,
    parse_risk_pct,
    parse_symbol,
    source_of,
    verify_symbol,
)
from sentinel.core.config import RiskConfig, Secrets, Settings, load_config
from sentinel.ingestion.models import InstrumentMeta
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import GateStatus, PortfolioState, RejectionReason
from tests.bot_double import owner_account
from tests.risk_double import PLAN_NOW, SOLUSDT, analyst_report, market_context

RISK = RiskConfig()


@pytest.fixture
def settings() -> Settings:
    return Settings(secrets=Secrets(_env_file=None), config=load_config())


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #


def test_a_db_watchlist_beats_the_yaml_one(settings: Settings) -> None:
    stored = {WATCHLIST: ["SOLUSDT", "INJUSDT"]}
    assert effective_config(settings, stored).watchlist == ("SOLUSDT", "INJUSDT")


def test_yaml_stands_when_nothing_is_stored(settings: Settings) -> None:
    assert effective_config(settings, {}).watchlist == settings.config.watchlist


def test_only_config_keys_become_overrides() -> None:
    """``capital_eur`` must never reach ``load_config``: ``AppConfig`` forbids
    unknown keys, so leaking it there would raise on every command."""
    overrides = config_overrides({CAPITAL_EUR: "10000", WATCHLIST: ["BTCUSDT"]})
    assert overrides == {WATCHLIST: ["BTCUSDT"]}


def test_an_override_does_not_disturb_the_rest_of_the_config(settings: Settings) -> None:
    merged = effective_config(settings, {WATCHLIST: ["SOLUSDT"]})
    assert merged.risk == settings.config.risk
    assert merged.costs == settings.config.costs


def test_source_of_labels_where_a_value_came_from() -> None:
    assert source_of(WATCHLIST, {WATCHLIST: ["BTCUSDT"]}) == "db"
    assert source_of(WATCHLIST, {}) == "yaml"


# --------------------------------------------------------------------------- #
# AccountState — what the gate actually reads
# --------------------------------------------------------------------------- #


def test_an_unset_capital_stays_none_so_the_gate_rejects_loudly(settings: Settings) -> None:
    """Substituting a default would size positions against a number the owner
    never chose. ``NO_CAPITAL`` is the correct, visible behaviour."""
    state = account_state(owner_account(), settings.config, Decimal("1.1593"))
    assert state.capital_eur is None

    decision = RiskEngine(settings.config).evaluate(
        report=analyst_report(),
        market=market_context(),
        account=state,
        portfolio=PortfolioState(),
    )
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.NO_CAPITAL


def test_stored_values_reach_the_gate(settings: Settings) -> None:
    state = account_state(
        owner_account(capital_eur=Decimal("8000"), risk_per_trade_pct=Decimal("1.25")),
        settings.config,
        Decimal("1.1593"),
    )
    assert state.capital_eur == Decimal("8000")
    assert state.risk_per_trade_pct == Decimal("1.25")


def test_the_config_default_applies_until_risk_is_set(settings: Settings) -> None:
    state = account_state(
        owner_account(capital_eur=Decimal("8000")), settings.config, Decimal("1.1593")
    )
    assert state.risk_per_trade_pct == settings.config.risk.risk_per_trade_pct


def test_capital_changes_do_not_touch_a_plan_already_issued(settings: Settings) -> None:
    """specs/RISK_ENGINE.md §7 — "open signals keep their original sizing"."""
    from sentinel.core.clock import FrozenClock

    engine = RiskEngine(settings.config, clock=FrozenClock(PLAN_NOW))
    first = engine.evaluate(
        report=analyst_report(),
        market=market_context(),
        account=account_state(
            owner_account(capital_eur=Decimal("10000")), settings.config, Decimal("1.1593")
        ),
        portfolio=PortfolioState(),
    )
    assert first.plan is not None
    issued = first.plan

    second = engine.evaluate(
        report=analyst_report(),
        market=market_context(),
        account=account_state(
            owner_account(capital_eur=Decimal("20000")), settings.config, Decimal("1.1593")
        ),
        portfolio=PortfolioState(),
    )
    assert second.plan is not None

    assert issued.capital_eur == Decimal("10000"), "the issued plan is immutable"
    assert second.plan.capital_eur == Decimal("20000")
    assert second.plan.risk_eur > issued.risk_eur


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("10000", "10000"), (" 10000 ", "10000"), ("€10,000.50", "10000.50"), ("0.01", "0.01")],
)
def test_capital_parsing_accepts_human_input(raw: str, expected: str) -> None:
    assert parse_capital(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "", "nan", "inf", "-inf"])
def test_capital_parsing_rejects_the_rest(raw: str) -> None:
    assert isinstance(parse_capital(raw), Invalid)


@pytest.mark.parametrize("raw", ["0.25", "1.5", "0.75%", " 1.00 "])
def test_risk_parsing_accepts_the_configured_band(raw: str) -> None:
    assert isinstance(parse_risk_pct(raw, RISK), Decimal)


@pytest.mark.parametrize("raw", ["0.2499", "1.5001", "0", "-0.5", "abc", "nan"])
def test_risk_parsing_rejects_outside_it(raw: str) -> None:
    assert isinstance(parse_risk_pct(raw, RISK), Invalid)


def test_the_risk_band_is_read_from_config_not_hardcoded() -> None:
    wider = RiskConfig(risk_per_trade_min_pct=Decimal("0.1"), risk_per_trade_max_pct=Decimal("5"))
    assert parse_risk_pct("3", wider) == Decimal("3")
    assert isinstance(parse_risk_pct("3", RISK), Invalid)


@pytest.mark.parametrize("raw", ["solusdt", " SOLUSDT ", "SolUsdt"])
def test_symbol_parsing_normalizes(raw: str) -> None:
    assert parse_symbol(raw) == "SOLUSDT"


@pytest.mark.parametrize("raw", ["SOL", "", "SOL-USDT", "SOL USDT", "A" * 30])
def test_symbol_parsing_rejects_nonsense(raw: str) -> None:
    assert isinstance(parse_symbol(raw), Invalid)


async def test_a_cached_instrument_answers_without_a_network_call() -> None:
    """Every symbol already ingested is verified from the DB, not the exchange."""

    class ExplodingChecker:
        async def instrument_meta(self, symbol: str) -> InstrumentMeta:
            raise AssertionError("must not be called when the symbol is already known")

    assert await verify_symbol("SOLUSDT", SOLUSDT, ExplodingChecker()) is None


async def test_an_unknown_symbol_is_checked_against_the_exchange() -> None:
    class Checker:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def instrument_meta(self, symbol: str) -> InstrumentMeta:
            self.asked.append(symbol)
            raise ValueError("binance does not list this market")

    checker = Checker()
    result = await verify_symbol("NOTREALUSDT", None, checker)
    assert checker.asked == ["NOTREALUSDT"]
    assert isinstance(result, Invalid)


async def test_no_checker_means_the_edit_still_goes_through() -> None:
    """An edit must not be blocked on a network the bot may not have."""
    assert await verify_symbol("INJUSDT", None, None) is None
