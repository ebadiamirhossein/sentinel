"""The forex signal card (FOREX.md §16.8).

Three claims, and the second is the one that would be easiest to get quietly wrong.

1. **It says what this market has.** Pips, the measured spread with its basis, rollover
   with its Wednesday tripling, the ESMA words beside the leverage.
2. **It does not say what this market does not have.** No liquidation buffer, no
   funding, no USDT notional — and, more importantly, the *absence* is replaced by a
   positive statement rather than left as a gap. A reader who has been trading crypto
   cards for 54 cycles looks for "Liq. buffer OK"; not finding it, and not being told
   why, is how an account-level stop-out becomes a surprise.
3. **It is a projection of the plan.** ``tests/bot/test_no_arithmetic.py`` covers this
   module too, from its first commit — that is why it is a module rather than a branch
   inside ``cards.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from sentinel.analyst.models import Direction, TimeframeLabel
from sentinel.bot.cards import signal_card
from sentinel.bot.forex_cards import ACCOUNT_LEVEL_MARGIN, WEEKEND_GAP, forex_signal_card
from sentinel.bot.formatting import money_eur
from sentinel.bot.models import SignalDecision, SignalRecord
from sentinel.core.config import AppConfig
from sentinel.core.markets import Market
from sentinel.fx.plan import ForexPlan
from tests.bot.telegram_html import assert_sendable
from tests.fx.forex_double import analyst_report, decide, forex_plan
from tests.risk_double import approved_plan

TZ = ZoneInfo("Europe/Vilnius")


@pytest.fixture
def plan(repo_config: AppConfig) -> ForexPlan:
    return forex_plan(repo_config)


@pytest.fixture
def record(plan: ForexPlan) -> SignalRecord:
    return SignalRecord(plan=plan, user_id=7222549221, number=7, market=Market.FOREX)


@pytest.fixture
def card(record: SignalRecord) -> str:
    return forex_signal_card(record, TZ)


# --------------------------------------------------------------------------- #
# What this market has
# --------------------------------------------------------------------------- #


def test_the_card_is_valid_telegram_html(card: str) -> None:
    assert_sendable(card)


def test_every_level_carries_its_distance_in_pips(card: str, plan: ForexPlan) -> None:
    """A four-decimal percentage on a 1.1690 quote is a number nobody converts at 3am."""
    assert f"{plan.stop_distance_pips} pips" in card
    for pips in plan.target_distances_pips:
        assert f"{pips} pips" in card
    for rung in plan.entries:
        assert f"{rung.distance_pips} pips" in card


def test_the_spread_appears_with_the_basis_it_was_measured_on(card: str, plan: ForexPlan) -> None:
    """§7.3 made the spread a measurement. "1.1 pips" alone would not say from where."""
    assert f"spread {plan.costs.spread_pips} pips" in card
    assert plan.costs.spread_basis in card
    assert "median for" in card


def test_the_leverage_never_appears_without_the_assumption(card: str, plan: ForexPlan) -> None:
    """§7.6: there is no MarginRates to read, so 30:1 is a documented assumption.

    The words travel on the plan precisely so a renderer cannot drop them, and this is
    the test that says a renderer did not.
    """
    assert f"Max leverage {plan.max_leverage}x" in card
    assert "ESMA" in card
    assert "documented assumption" in card


def test_margin_is_shown_as_a_share_of_equity(card: str, plan: ForexPlan) -> None:
    assert f"{plan.margin_pct_of_equity}% of equity" in card


def test_the_quantity_is_labelled_with_the_base_currency(card: str, plan: ForexPlan) -> None:
    """ "26,628.10" alone is a number without a unit; "26628.10 EUR" is a position."""
    assert f"{plan.entries[0].qty} EUR" in card


def test_the_expiry_says_which_of_its_two_deadlines_it_was(card: str, plan: ForexPlan) -> None:
    """§5.4 gives a pending ladder a TTL and a Friday close. One timestamp cannot say."""
    assert plan.expiry_basis in card
    assert plan.expiry_basis != ""


def test_rollover_is_never_called_funding(card: str) -> None:
    """§2's ledger replaces one with the other and they do not behave alike."""
    assert "funding" not in card.lower()
    assert "rollover" in card.lower()


def _held_overnight(config: AppConfig) -> str:
    """A swing idea placed on a Thursday, so it crosses at least one 21:00 UTC."""
    decision = decide(
        config,
        report=analyst_report(timeframe_label=TimeframeLabel.SWING),
        now=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    )
    assert decision.plan is not None
    assert decision.plan.costs.rollover_nights > 0
    return forex_signal_card(
        SignalRecord(plan=decision.plan, user_id=1, number=1, market=Market.FOREX), TZ
    )


def test_an_unpriced_rollover_says_so_rather_than_showing_zero(repo_config: AppConfig) -> None:
    """CLAUDE.md: degrade explicitly, never fabricate — and this is the shipped state.

    ``forex.swap_pips_per_night`` is empty because swap rates are published by the
    broker rather than derivable from a chart (§7.3). A configured zero and a measured
    zero both render "€0.00", and only one of them means the trade is free to hold.
    """
    assert repo_config.forex.swap_pips_per_night == {}

    card = _held_overnight(repo_config)

    assert "rollover <b>not priced</b>" in card
    assert "no swap rate configured" in card


def test_a_priced_rollover_states_the_wednesday_tripling(repo_config: AppConfig) -> None:
    """The non-vacuity sibling — and the branch that runs once the owner fills the table.

    Rollover is charged at 21:00 UTC and TRIPLED on Wednesday. That is a fact about the
    instrument, not a detail, and a reader holding a position over a Wednesday needs it.
    """
    forex = repo_config.forex.model_copy(
        update={"swap_pips_per_night": {"EURUSD": {"long": Decimal("0.4")}}}
    )
    config = repo_config.model_copy(update={"forex": forex})

    card = _held_overnight(config)

    assert "tripled on Wednesday" in card
    assert "not priced" not in card


# --------------------------------------------------------------------------- #
# What this market does not have — stated, not merely missing
# --------------------------------------------------------------------------- #


def test_the_card_states_that_stop_out_risk_is_account_level(card: str) -> None:
    """The positive statement that replaces crypto's liquidation-buffer line (§7.6).

    Leaving a gap where a reader expects one is the failure this project has met three
    times under the name silence-as-success.
    """
    assert ACCOUNT_LEVEL_MARGIN in card
    assert "account-level" in card
    assert "no liquidation price" in card


def test_no_crypto_concept_leaks_onto_a_forex_card(card: str) -> None:
    for absent in ("USDT", "Liq. buffer", "isolated", "maker", "taker"):
        assert absent not in card, absent


def test_the_weekend_gap_warning_appears_only_when_it_applies(
    card: str, repo_config: AppConfig
) -> None:
    """A warning that cries wolf on every card is a warning nobody reads."""
    assert WEEKEND_GAP not in card  # a Wednesday intraday idea does not reach Friday

    thursday = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    late = decide(
        repo_config, report=analyst_report(timeframe_label=TimeframeLabel.SWING), now=thursday
    )
    assert late.plan is not None
    assert late.plan.weekend_gap_warning is True
    assert WEEKEND_GAP in forex_signal_card(
        SignalRecord(plan=late.plan, user_id=1, number=1, market=Market.FOREX), TZ
    )


# --------------------------------------------------------------------------- #
# The market tag, under §11's condition
# --------------------------------------------------------------------------- #


def test_the_tag_is_off_by_default(card: str) -> None:
    """Same default as the crypto card: a caller that forgets cannot tag a card."""
    assert "FOREX ·" not in card


def test_the_tag_appears_when_asked(record: SignalRecord) -> None:
    assert "FOREX · EURUSD" in forex_signal_card(record, TZ, show_market=True)


def test_the_tagged_card_differs_by_the_tag_alone(record: SignalRecord) -> None:
    """specs/TELEGRAM_UX.md §3e, asserted rather than assumed."""
    plain = forex_signal_card(record, TZ)
    tagged = forex_signal_card(record, TZ, show_market=True)

    assert tagged.replace("FOREX · ", "", 1) == plain


def test_a_decision_is_echoed_back_in_words(plan: ForexPlan) -> None:
    decided = SignalRecord(
        plan=plan,
        user_id=1,
        number=1,
        market=Market.FOREX,
        decision=SignalDecision.TAKEN,
    )

    assert "Your call: ✅ Taken" in forex_signal_card(decided, TZ)


# --------------------------------------------------------------------------- #
# The two renderers refuse each other's plans
# --------------------------------------------------------------------------- #


def test_the_crypto_renderer_refuses_a_forex_plan(record: SignalRecord) -> None:
    """Loud, rather than most of a card with three wrong lines in it (§16.7)."""
    with pytest.raises(TypeError, match="dispatched on"):
        signal_card(record, TZ)


def test_the_forex_renderer_refuses_a_crypto_plan(repo_config: AppConfig) -> None:
    crypto = SignalRecord(
        plan=approved_plan(repo_config), user_id=1, number=1, market=Market.CRYPTO
    )

    with pytest.raises(TypeError, match="dispatched on"):
        forex_signal_card(crypto, TZ)


# --------------------------------------------------------------------------- #
# A short, so nothing passes by assuming a long
# --------------------------------------------------------------------------- #


def test_a_short_renders_its_own_side(repo_config: AppConfig) -> None:
    decision = decide(
        repo_config,
        report=analyst_report(
            direction=Direction.SHORT,
            zone=("1.16900", "1.17000"),
            stop="1.17300",
            targets=("1.16000", "1.15500", "1.14800"),
        ),
    )
    assert decision.plan is not None
    card = forex_signal_card(
        SignalRecord(plan=decision.plan, user_id=1, number=1, market=Market.FOREX), TZ
    )

    assert "🔴 SHORT" in card
    # Targets are below the entry and the card still signs their distance with a "+":
    # a short's reward is a falling price, and a minus sign there reads as a loss.
    assert decision.plan.targets[0] < decision.plan.avg_entry
    assert f"+{decision.plan.target_distances_pct[0]}%" in card


def test_the_actual_risk_is_never_above_the_planned_budget(plan: ForexPlan, card: str) -> None:
    """§7.2: under-risking is fine, over-risking is not — and the card shows both."""
    assert plan.risk_eur <= plan.planned_risk_eur
    assert f"Actual risk €{plan.risk_eur} (planned €{plan.planned_risk_eur})" in card


def test_the_card_shows_the_engine_figures_verbatim(plan: ForexPlan, card: str) -> None:
    """Spot-check that quantized values arrive unrounded and unreformatted."""
    for value in (
        plan.risk_eur,
        plan.notional_eur,
        plan.margin_eur,
        plan.stop_distance_pct,
        plan.stop_distance_pips,
        plan.costs.cost_pct_of_risk,
        plan.avg_entry,
        plan.avg_fill_price,
        plan.last_price,
        plan.pip_value_eur,
        *plan.target_distances_pct,
        *plan.rr_targets,
        *plan.rr_targets_net,
    ):
        assert str(value) in card, f"{value} is on the plan but not on the card"

    # The cost total is the one figure whose SCALE the card changes.
    # ``fx/rounding.cost_money`` keeps four decimals because at €200 cents-rounding a
    # spread moves net RR by 0.01R, and that precision is what fed the gate. The card
    # shows money. Value preserved, scale fixed — the §16.8 / defect #22 rule.
    assert f"€{money_eur(plan.costs.total_eur)}" in card
    assert str(plan.costs.total_eur) not in card


def test_a_decimal_never_reaches_the_card_in_scientific_notation(card: str) -> None:
    """The ``2E+1`` hazard ``percent()`` exists to prevent, asserted on a real render."""
    assert "E+" not in card
    assert "E-" not in card
