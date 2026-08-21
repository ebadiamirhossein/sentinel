"""The forex quality thresholds, and the one that is NOT crypto's (spec defect #21).

FOREX.md §7 and §9 name the sizing rails and the correlation cap and say nothing about
setup *quality*, so the obvious move was to read crypto's ``RiskConfig``. One of those
numbers is actively wrong here, and the load-bearing test in this file is the one that
says so out loud rather than the ones that read a default back.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from sentinel.core.config import AppConfig, ForexConfig, load_config


@pytest.fixture
def forex(repo_config: AppConfig) -> ForexConfig:
    return repo_config.forex


# --------------------------------------------------------------------------- #
# Defect #21 — the number that does not transfer
# --------------------------------------------------------------------------- #


def test_the_forex_entry_distance_bound_is_not_cryptos(repo_config: AppConfig) -> None:
    """EURUSD moves ~0.5% in a day; crypto's 3.0 could essentially never fire.

    A rail that cannot fire is worse than an absent one, because it reads on a
    checklist as a rail. This asserts the two are *different*, not that forex's is
    0.5 — so tightening or loosening it during DRY_RUN calibration does not have to
    edit a test whose point is the comparison.
    """
    assert repo_config.forex.max_entry_distance_pct < repo_config.risk.max_entry_distance_pct


def test_a_forex_entry_bound_admits_a_realistic_eurusd_zone(forex: ForexConfig) -> None:
    """The non-vacuity half: the bound has to admit a real setup, not just reject.

    A 1.1700 entry 12 pips below a 1.1712 last price is 0.10% away — an ordinary
    pullback entry, and it must pass. 60 pips away (0.51%) must not.
    """
    last = Decimal("1.1712")
    near_pct = (last - Decimal("1.1700")) / last * Decimal("100")
    far_pct = (last - Decimal("1.1652")) / last * Decimal("100")

    assert near_pct < forex.max_entry_distance_pct
    assert far_pct > forex.max_entry_distance_pct


def test_both_markets_gate_on_the_same_net_rr(repo_config: AppConfig) -> None:
    """Deliberate: two markets gated at the same NET level are comparable in /stats."""
    assert repo_config.forex.min_rr_tp1 == repo_config.risk.min_rr_tp1


def test_forex_caps_daily_signals_below_crypto(repo_config: AppConfig) -> None:
    """Three instruments, all crossing the dollar — see §9."""
    assert repo_config.forex.max_signals_per_day < repo_config.risk.max_signals_per_day


# --------------------------------------------------------------------------- #
# §5.4's ordering promise is checked, not trusted
# --------------------------------------------------------------------------- #


def test_the_shipped_ladder_expiry_is_before_the_shipped_close(forex: ForexConfig) -> None:
    assert forex.friday_ladder_expiry_hour_utc < forex.week_close_hour_utc


@pytest.mark.parametrize("expiry_hour", [21, 22, 23])
def test_a_ladder_expiry_at_or_after_the_close_is_refused(expiry_hour: int) -> None:
    """The failure this guards is silent: the market shuts, the tracker stops ticking,
    and the ladder that was supposed to expire before the close is still pending on
    Sunday evening carrying the gap risk §5.4 exists to remove."""
    with pytest.raises(ValidationError, match="must be BEFORE"):
        ForexConfig(friday_ladder_expiry_hour_utc=expiry_hour, week_close_hour_utc=21)


def test_the_defaults_construct(repo_config: AppConfig) -> None:
    """A bare ForexConfig must satisfy its own validator — otherwise every test that
    builds one has to know the ordering rule."""
    assert ForexConfig().friday_ladder_expiry_hour_utc < ForexConfig().week_close_hour_utc
    assert repo_config.forex == load_config().forex
