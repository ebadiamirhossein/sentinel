"""Two look-alike plans must never be confusable (FOREX.md §16.7, owner ruling I3).

``SignalRecord.plan`` is ``TradePlan | ForexPlan``, and :class:`ForexPlan` **mirrors**
``TradePlan``'s shared vocabulary on purpose — that is what lets one signals table, one
publisher and one tracker serve both markets with no translation layer.

The mirroring is also the hazard, and it is the hazard shape this project already has a
scar from: **the pip derivation**. There, ``10 ** -(decimals - 1)`` and
``10 ** -decimals`` both produce plausible numbers, and the wrong one is wrong by 10x
with no exception anywhere. Here, a mis-dispatched rehydration could produce a plausible
*object* — most of a plan, with three numbers meaning something else.

So this file asserts three things, and the third is the one that survives future edits:

1. a forex row cannot validate as a ``TradePlan``;
2. a crypto row cannot validate as a ``ForexPlan``;
3. **neither model's field set is a subset of the other's** — which is the property (1)
   and (2) actually rest on. Pinning the property rather than the two examples is what
   keeps this true after somebody adds a field.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from sentinel.bot.models import SignalRecord
from sentinel.bot.plans import (
    PLAN_MODELS,
    eur_quote_rate_of,
    plan_model_for,
    plan_of,
    qty_step_of,
    rungs_of,
)
from sentinel.core.config import AppConfig
from sentinel.core.markets import Market
from sentinel.fx.plan import ForexEntryRung, ForexPlan
from sentinel.risk.models import EntryRung, TradePlan
from tests.fx.forex_double import forex_plan
from tests.risk_double import approved_plan

# --------------------------------------------------------------------------- #
# The structural property everything else rests on
# --------------------------------------------------------------------------- #


def test_neither_plan_is_a_subset_of_the_other() -> None:
    """The property, not the examples.

    If ``ForexPlan``'s fields were ever a subset of ``TradePlan``'s, a forex payload
    would validate as a ``TradePlan`` — silently, with ``notional_usdt`` defaulted or
    missing — and the two round-trip tests below would still pass on their specific
    fixtures right up until the day they did not. This fails on the *addition* instead.
    """
    crypto = set(TradePlan.model_fields)
    forex = set(ForexPlan.model_fields)

    assert not forex <= crypto, f"ForexPlan became a subset of TradePlan: {crypto - forex}"
    assert not crypto <= forex, f"TradePlan became a subset of ForexPlan: {forex - crypto}"


def test_neither_rung_is_a_subset_of_the_other() -> None:
    """The same property one level down — ``entries`` is where the payloads diverge first."""
    crypto = set(EntryRung.model_fields)
    forex = set(ForexEntryRung.model_fields)

    assert not forex <= crypto
    assert not crypto <= forex


def test_the_two_plans_share_the_vocabulary_the_storage_layer_reads() -> None:
    """The other half of §16.2: mirroring is a requirement, not an accident.

    ``storage.repositories.signal_row`` addresses a plan by these names and its body did
    not change when forex arrived. A rename on either side breaks one table's writes for
    one market, which is the kind of thing that shows up as a missing signal.
    """
    shared = {
        "plan_id",
        "created_at",
        "symbol",
        "direction",
        "setup_type",
        "confidence",
        "report",
        "entries",
        "avg_entry",
        "avg_fill_price",
        "stop",
        "targets",
        "expires_at",
        "planned_risk_eur",
        "risk_eur",
        "capital_eur",
    }
    assert shared <= set(TradePlan.model_fields)
    assert shared <= set(ForexPlan.model_fields)


# --------------------------------------------------------------------------- #
# Both directions fail loudly
# --------------------------------------------------------------------------- #


def test_a_forex_payload_cannot_rehydrate_as_a_crypto_plan(repo_config: AppConfig) -> None:
    payload = forex_plan(repo_config).model_dump(mode="json")

    with pytest.raises(ValidationError) as raised:
        TradePlan.model_validate(payload)

    reported = str(raised.value)
    assert "notional_usdt" in reported
    assert "liq_buffer_ok" in reported


def test_a_crypto_payload_cannot_rehydrate_as_a_forex_plan(repo_config: AppConfig) -> None:
    payload = approved_plan(repo_config).model_dump(mode="json")

    with pytest.raises(ValidationError) as raised:
        ForexPlan.model_validate(payload)

    reported = str(raised.value)
    assert "pip" in reported
    assert "quote_currency" in reported


# --------------------------------------------------------------------------- #
# plan_of dispatches on the market and never guesses
# --------------------------------------------------------------------------- #


def test_plan_of_returns_the_model_the_market_names(repo_config: AppConfig) -> None:
    crypto = approved_plan(repo_config)
    forex = forex_plan(repo_config)

    assert isinstance(plan_of(crypto.model_dump(mode="json"), Market.CRYPTO), TradePlan)
    assert isinstance(plan_of(forex.model_dump(mode="json"), Market.FOREX), ForexPlan)


def test_a_mismatched_market_raises_rather_than_falling_back(repo_config: AppConfig) -> None:
    """The forbidden convenience, asserted absent.

    A ``try TradePlan / except try ForexPlan`` fallback would make this call *succeed*,
    which is the mechanism that turns a mis-stamped row into a plausible plan. The
    useful outcome is a loud failure on one signal.
    """
    forex = forex_plan(repo_config).model_dump(mode="json")

    with pytest.raises(ValidationError):
        plan_of(forex, Market.CRYPTO)


def test_every_market_names_a_plan_model() -> None:
    """A market with no registered model is a KeyError at the seam, not a guess."""
    assert set(PLAN_MODELS) == set(Market)
    for market in Market:
        assert plan_model_for(market) in (TradePlan, ForexPlan)


# --------------------------------------------------------------------------- #
# The accessors that let one tracker serve both
# --------------------------------------------------------------------------- #


def test_the_euro_rate_accessor_reads_each_markets_own_field(repo_config: AppConfig) -> None:
    """One concept, two right names — and on USDJPY the forex one is EURJPY (§7.1)."""
    crypto = approved_plan(repo_config)
    forex = forex_plan(repo_config)

    assert eur_quote_rate_of(crypto) == crypto.eurusd_rate
    assert eur_quote_rate_of(forex) == forex.eur_quote_rate


def test_the_quantity_step_accessor_converts_saxos_precision(repo_config: AppConfig) -> None:
    """Saxo publishes ``AmountDecimals``; Binance publishes a ``qty_step``.

    Same quantity, two ways of stating it. The accessor is what lets the state
    machine's ``floor_to_step`` stay one line rather than a branch.
    """
    forex = forex_plan(repo_config)
    crypto = approved_plan(repo_config)

    assert qty_step_of(forex) == Decimal("0.01")  # AmountDecimals 2
    assert qty_step_of(crypto) == crypto.instrument.qty_step


def test_rungs_of_exposes_the_same_shape_for_both(repo_config: AppConfig) -> None:
    for plan in (approved_plan(repo_config), forex_plan(repo_config)):
        for rung in rungs_of(plan):
            assert rung.price > 0
            assert rung.qty > 0
            assert rung.notional_eur > 0


def test_a_forex_record_is_a_valid_signal_record(repo_config: AppConfig) -> None:
    """The contract change itself: ``SignalRecord`` accepts either plan."""
    record = SignalRecord(plan=forex_plan(repo_config), user_id=1, number=1, market=Market.FOREX)
    assert isinstance(record.plan, ForexPlan)
