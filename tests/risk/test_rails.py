"""§2 rule 7 and §7 — portfolio rails and pause state.

The engine owns the arithmetic; the *state* it reads (open risk, positions,
cooldowns, pause) is supplied by the caller and filled from the DB by M7.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import (
    GateStatus,
    PauseReason,
    PauseState,
    PortfolioState,
    RejectionReason,
)
from sentinel.risk.rails import check_portfolio_rails, evaluate_daily_loss

from .conftest import NOW, account, market, portfolio, report


def rails(
    config: AppConfig,
    *,
    pf: PortfolioState | None = None,
    symbol: str = "SOLUSDT",
    risk_pct: Decimal = Decimal("0.75"),
    now: datetime | None = None,
) -> RejectionReason | None:
    return check_portfolio_rails(
        portfolio=pf if pf is not None else portfolio(),
        symbol=symbol,
        risk_per_trade_pct=risk_pct,
        config=config.risk,
        now=now if now is not None else NOW,
    )


# --------------------------------------------------------------------------- #
# Open-risk budget
# --------------------------------------------------------------------------- #


def test_open_risk_plus_this_trade_must_stay_inside_the_budget(config: AppConfig) -> None:
    # 2.25% budget, 0.75% per trade: three open trades fill it exactly.
    assert rails(config, pf=portfolio(open_risk_pct="1.5")) is None
    assert rails(config, pf=portfolio(open_risk_pct="1.6")) is RejectionReason.MAX_OPEN_RISK


def test_open_risk_exactly_at_the_budget_is_allowed(config: AppConfig) -> None:
    """2.25 is 'less than or equal' per §2 rule 7 — not a rejection."""
    assert rails(config, pf=portfolio(open_risk_pct="1.5"), risk_pct=Decimal("0.75")) is None


def test_position_count_is_capped(config: AppConfig) -> None:
    assert rails(config, pf=portfolio(open_positions=3)) is None
    assert rails(config, pf=portfolio(open_positions=4)) is RejectionReason.MAX_POSITIONS


# --------------------------------------------------------------------------- #
# Cooldown
# --------------------------------------------------------------------------- #


def test_symbol_on_cooldown_is_rejected(config: AppConfig) -> None:
    cooldowns = {"SOLUSDT": NOW + timedelta(hours=1)}
    assert rails(config, pf=portfolio(cooldowns=cooldowns)) is RejectionReason.SYMBOL_COOLDOWN


def test_expired_cooldown_lets_the_symbol_through(config: AppConfig) -> None:
    cooldowns = {"SOLUSDT": NOW - timedelta(seconds=1)}
    assert rails(config, pf=portfolio(cooldowns=cooldowns)) is None


def test_another_symbols_cooldown_is_irrelevant(config: AppConfig) -> None:
    cooldowns = {"BTCUSDT": NOW + timedelta(hours=1)}
    assert rails(config, pf=portfolio(cooldowns=cooldowns)) is None


# --------------------------------------------------------------------------- #
# Pause
# --------------------------------------------------------------------------- #


def test_manual_pause_blocks_new_signals(config: AppConfig) -> None:
    paused = PauseState(paused=True, reason=PauseReason.MANUAL)
    assert rails(config, pf=portfolio(pause=paused)) is RejectionReason.PAUSED


def test_loss_limit_pause_expires_on_its_own(config: AppConfig) -> None:
    paused = PauseState(
        paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=NOW - timedelta(seconds=1)
    )
    assert rails(config, pf=portfolio(pause=paused)) is None


def test_loss_limit_pause_blocks_until_it_expires(config: AppConfig) -> None:
    paused = PauseState(
        paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=NOW + timedelta(hours=23)
    )
    assert rails(config, pf=portfolio(pause=paused)) is RejectionReason.PAUSED


def test_manual_pause_without_an_expiry_never_lapses(config: AppConfig) -> None:
    paused = PauseState(paused=True, reason=PauseReason.MANUAL, until=None)
    assert rails(config, pf=portfolio(pause=paused), now=NOW + timedelta(days=30)) is (
        RejectionReason.PAUSED
    )


# --------------------------------------------------------------------------- #
# §7 — daily loss limit auto-pause
# --------------------------------------------------------------------------- #


def test_loss_below_the_limit_does_not_pause(config: AppConfig) -> None:
    state = evaluate_daily_loss(
        realized_loss_pct=Decimal("2.9"), limit_pct=config.risk.daily_loss_limit_pct, now=NOW
    )
    assert state.paused is False
    assert state.reason is None


def test_loss_at_the_limit_pauses_for_24h(config: AppConfig) -> None:
    state = evaluate_daily_loss(
        realized_loss_pct=Decimal("3.0"), limit_pct=config.risk.daily_loss_limit_pct, now=NOW
    )
    assert state.paused is True
    assert state.reason is PauseReason.DAILY_LOSS_LIMIT
    assert state.until == NOW + timedelta(hours=24)


def test_a_manual_pause_is_not_overwritten_by_a_quiet_day(config: AppConfig) -> None:
    """Only the loss limit sets a loss pause; a calm day must not clear /pause."""
    existing = PauseState(paused=True, reason=PauseReason.MANUAL)
    state = evaluate_daily_loss(
        realized_loss_pct=Decimal("0"),
        limit_pct=config.risk.daily_loss_limit_pct,
        now=NOW,
        current=existing,
    )
    assert state == existing


# --------------------------------------------------------------------------- #
# Through the gate
# --------------------------------------------------------------------------- #


def test_rails_reject_before_any_sizing_happens(config: AppConfig, clock: FrozenClock) -> None:
    engine = RiskEngine(config, clock=clock)
    decision = engine.evaluate(
        report=report(),
        market=market(),
        account=account(),
        portfolio=portfolio(open_positions=4),
    )
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.MAX_POSITIONS
    assert decision.plan is None
