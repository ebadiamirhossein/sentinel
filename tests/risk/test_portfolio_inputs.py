"""Building ``PortfolioState`` from what the database holds (§2 rule 7, §7).

M4 left ``PortfolioState`` a plain input and said M7 would fill it. These are the
three conversions that filling it actually needs, and they live in
``sentinel/risk/`` rather than in the orchestrator for the reason rule zero
gives: they decide whether a signal is allowed, so they are risk math, and risk
math is pure, ``Decimal`` and 100%-branch-covered.

The *selection* stays outside — which signals count as open, which resolutions
arm a cooldown — because that is a query. What is here is the arithmetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sentinel.risk.rails import cooldown_until, open_risk_pct, realized_loss_pct

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Open risk
# --------------------------------------------------------------------------- #


def test_open_risk_is_the_sum_of_the_risk_each_open_signal_was_issued_with() -> None:
    """Each plan's own ``risk_per_trade_pct``, not today's setting.

    §7: "Capital changes via /capital apply to new signals only; open signals keep
    their original sizing." The same holds for the risk percentage — a signal
    issued at 0.75% still occupies 0.75% of the budget after /risk 1.0.
    """
    assert open_risk_pct([Decimal("0.75"), Decimal("0.75"), Decimal("1.0")]) == Decimal("2.50")


def test_no_open_signals_is_zero_risk_not_an_error() -> None:
    assert open_risk_pct([]) == Decimal("0")


def test_open_risk_stays_decimal() -> None:
    """Summing into a float here would poison the §2 rule 7 comparison."""
    assert isinstance(open_risk_pct([Decimal("0.75")]), Decimal)


# --------------------------------------------------------------------------- #
# Cooldowns
# --------------------------------------------------------------------------- #


def test_a_resolution_arms_a_cooldown_for_the_configured_hours() -> None:
    until = cooldown_until([("SOLUSDT", NOW)], hours=4)
    assert until == {"SOLUSDT": NOW + timedelta(hours=4)}


def test_the_most_recent_resolution_for_a_symbol_wins() -> None:
    """Two stop-outs in a day must not let the older one shorten the cooldown."""
    older = NOW - timedelta(hours=3)
    until = cooldown_until([("SOLUSDT", older), ("SOLUSDT", NOW)], hours=4)
    assert until == {"SOLUSDT": NOW + timedelta(hours=4)}

    # Order of arrival must not matter — a query is not required to sort.
    assert cooldown_until([("SOLUSDT", NOW), ("SOLUSDT", older)], hours=4) == until


def test_symbols_are_independent() -> None:
    until = cooldown_until([("SOLUSDT", NOW), ("ETHUSDT", NOW - timedelta(hours=1))], hours=4)
    assert until["SOLUSDT"] == NOW + timedelta(hours=4)
    assert until["ETHUSDT"] == NOW + timedelta(hours=3)


def test_no_resolutions_means_no_cooldowns() -> None:
    assert cooldown_until([], hours=4) == {}


def test_a_zero_hour_cooldown_disables_the_rail_rather_than_pinning_it_to_now() -> None:
    """A configured 0 must mean "no cooldown", not "expires exactly now" — the
    latter is a race that depends on which side of the microsecond the tick
    lands on."""
    assert cooldown_until([("SOLUSDT", NOW)], hours=0) == {}


# --------------------------------------------------------------------------- #
# Realized daily loss
# --------------------------------------------------------------------------- #


def test_a_losing_day_reports_a_positive_magnitude() -> None:
    """``evaluate_daily_loss`` takes "3.1 means down 3.1%", so the sign is dropped
    here and nowhere else."""
    assert realized_loss_pct(
        [Decimal("-150"), Decimal("-160")], capital_eur=Decimal("10000")
    ) == Decimal("3.1")


def test_a_profitable_day_is_zero_loss_not_a_negative_one() -> None:
    """A negative "loss" would compare as < limit and read as safety, but it would
    also silently mean the rail had been fed a profit as if it were a drawdown."""
    assert realized_loss_pct([Decimal("220")], capital_eur=Decimal("10000")) == Decimal("0")


def test_wins_and_losses_net_off_within_the_day() -> None:
    """§7 limits the *realized* daily loss, so a recovery counts. Two stops and a
    winner that gives most of it back is not a 3% day."""
    assert realized_loss_pct(
        [Decimal("-150"), Decimal("-150"), Decimal("270")], capital_eur=Decimal("10000")
    ) == Decimal("0.3")


def test_a_flat_day_is_zero() -> None:
    assert realized_loss_pct([], capital_eur=Decimal("10000")) == Decimal("0")


def test_without_capital_there_is_no_percentage_to_report() -> None:
    """``capital_eur`` is unset until /capital. The gate already rejects every
    signal with NO_CAPITAL in that state, so there is nothing to pause; inventing
    a denominator would be the fabrication CLAUDE.md forbids."""
    assert realized_loss_pct([Decimal("-150")], capital_eur=None) == Decimal("0")
    assert realized_loss_pct([Decimal("-150")], capital_eur=Decimal("0")) == Decimal("0")
