"""§4 sizing, rounding, leverage and the liquidation-buffer rule."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.analyst.models import Direction
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import GateStatus, LadderRung, RejectionReason
from sentinel.risk.rounding import (
    ceil_to_int,
    ceil_to_tick,
    floor_to_step,
    floor_to_tick,
    round_stop_to_tick,
    round_to_tick,
)
from sentinel.risk.sizing import (
    planned_qty,
    risk_budget_eur,
    solve_leverage,
    stop_distance_fraction,
    weighted_avg_entry,
)

from .conftest import BTCUSDT, account, market, portfolio, report

# --------------------------------------------------------------------------- #
# Rounding
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "tick", "expected"),
    [
        ("82.117", "0.01", "82.12"),
        ("82.114", "0.01", "82.11"),
        ("82.115", "0.01", "82.12"),  # ties away from zero, deterministically
        ("64255.14", "0.1", "64255.1"),
        ("82.10", "0.01", "82.10"),
    ],
)
def test_round_to_tick(value: str, tick: str, expected: str) -> None:
    assert round_to_tick(Decimal(value), Decimal(tick)) == Decimal(expected)


@pytest.mark.parametrize(
    ("qty", "step", "expected"),
    [
        ("0.39518", "0.01", "0.39"),
        ("0.001999", "0.001", "0.001"),
        ("1.5", "1", "1"),
        ("0.0009", "0.001", "0"),  # below one step: no position at all
    ],
)
def test_quantities_always_round_down(qty: str, step: str, expected: str) -> None:
    assert floor_to_step(Decimal(qty), Decimal(step)) == Decimal(expected)


def test_stop_rounds_away_from_the_entry() -> None:
    """A tick must never tighten a stop the analyst chose (§2 rule 3's concern)."""
    assert round_stop_to_tick(Decimal("81.204"), Direction.LONG, Decimal("0.01")) == Decimal(
        "81.20"
    )
    assert round_stop_to_tick(Decimal("81.206"), Direction.LONG, Decimal("0.01")) == Decimal(
        "81.20"
    )
    assert round_stop_to_tick(Decimal("86.101"), Direction.SHORT, Decimal("0.01")) == Decimal(
        "86.11"
    )


def test_a_degenerate_grid_is_passed_through_rather_than_dividing_by_zero() -> None:
    """Bad exchange metadata must not crash the gate."""
    zero = Decimal("0")
    assert round_to_tick(Decimal("82.117"), zero) == Decimal("82.117")
    assert floor_to_tick(Decimal("82.117"), zero) == Decimal("82.117")
    assert ceil_to_tick(Decimal("82.117"), zero) == Decimal("82.117")
    assert floor_to_step(Decimal("0.395"), zero) == Decimal("0.395")


def test_ceil_to_int_is_the_leverage_step() -> None:
    assert ceil_to_int(Decimal("4.19")) == 5
    assert ceil_to_int(Decimal("4.00")) == 4
    assert ceil_to_int(Decimal("0.4")) == 1


# --------------------------------------------------------------------------- #
# Weighted entry and notional
# --------------------------------------------------------------------------- #


def test_weighted_average_entry() -> None:
    rungs = (
        LadderRung(price=Decimal("83.10"), weight_pct=Decimal("40")),
        LadderRung(price=Decimal("82.60"), weight_pct=Decimal("35")),
        LadderRung(price=Decimal("82.10"), weight_pct=Decimal("25")),
    )
    # 33.24 + 28.91 + 20.525
    assert weighted_avg_entry(rungs) == Decimal("82.675")


def test_risk_budget_is_a_percentage_of_capital() -> None:
    assert risk_budget_eur(Decimal("10000"), Decimal("0.75")) == Decimal("75.00")


def test_stop_distance_is_relative_to_the_weighted_entry() -> None:
    frac = stop_distance_fraction(Decimal("82.675"), Decimal("81.20"))
    assert frac == pytest.approx(Decimal("0.0178409"), abs=Decimal("1e-7"))


def test_each_rung_carries_its_stated_share_of_the_risk_budget() -> None:
    """Owner ruling: the 40/35/25 weights are shares of RISK, so §3's tracker rule
    ('rung 1 only + stop = -0.40R') and §8.5 are literally true."""
    risk_usdt = Decimal("86.9475")
    stop = Decimal("81.20")
    for weight, price in (("40", "83.10"), ("35", "82.60"), ("25", "82.10")):
        qty = planned_qty(
            weight_pct=Decimal(weight), price=Decimal(price), stop=stop, risk_usdt=risk_usdt
        )
        loss = qty * (Decimal(price) - stop)
        assert loss == pytest.approx(
            risk_usdt * Decimal(weight) / Decimal("100"), abs=Decimal("1e-9")
        )


def test_a_rung_sitting_on_the_stop_sizes_to_zero_rather_than_dividing() -> None:
    """It can risk nothing, so it takes nothing — and is collapsed away later."""
    assert planned_qty(
        weight_pct=Decimal("40"),
        price=Decimal("81.20"),
        stop=Decimal("81.20"),
        risk_usdt=Decimal("86.9475"),
    ) == Decimal(0)


def test_a_single_entry_still_satisfies_the_spec_notional_identity() -> None:
    """§4's `notional = risk / stop_dist_pct` is exact for one rung — pin it."""
    price, stop, risk = Decimal("82.10"), Decimal("81.56"), Decimal("100")
    qty = planned_qty(weight_pct=Decimal("100"), price=price, stop=stop, risk_usdt=risk)
    expected = risk / stop_distance_fraction(price, stop)
    assert qty * price == pytest.approx(expected, abs=Decimal("1e-6"))


# --------------------------------------------------------------------------- #
# Leverage: ceil to the step, clamp, then satisfy the liquidation buffer
# --------------------------------------------------------------------------- #


def solve(
    notional: str, margin_budget: str, stop_frac: str, *, max_leverage: int = 10
) -> int | None:
    return solve_leverage(
        notional_eur=Decimal(notional),
        margin_budget_eur=Decimal(margin_budget),
        stop_distance_fraction=Decimal(stop_frac),
        max_leverage=max_leverage,
        liq_buffer_multiple=Decimal("2.0"),
    )


def test_leverage_is_ceiled_to_the_next_whole_step() -> None:
    assert solve("4190", "1000", "0.0179") == 5


def test_leverage_is_clamped_to_the_configured_maximum() -> None:
    assert solve("50000", "1000", "0.005") == 10


def test_leverage_never_falls_below_one() -> None:
    assert solve("500", "1000", "0.02") == 1


def test_leverage_is_reduced_until_the_liquidation_buffer_holds() -> None:
    """stop 6% away needs liq >= 12%, so leverage cannot exceed 8x."""
    assert solve("10000", "1000", "0.06") == 8


def test_a_zero_stop_distance_is_unsizeable() -> None:
    assert solve("10000", "1000", "0") is None


def test_without_a_margin_budget_the_cap_and_the_buffer_decide() -> None:
    """margin_budget_pct = 0 would divide by zero; start from the cap instead."""
    assert solve("10000", "0", "0.01") == 10
    assert solve("10000", "0", "0.20") == 2


def test_leverage_below_one_is_a_rejection_not_a_rounding() -> None:
    """A stop more than half the entry away can never satisfy 2x liq buffer."""
    assert solve("1000", "1000", "0.51") is None


def test_liquidation_buffer_holds_for_every_solved_leverage() -> None:
    for stop_frac in ("0.005", "0.0179", "0.03", "0.06", "0.12", "0.24"):
        leverage = solve("10000", "1000", stop_frac)
        assert leverage is not None
        assert Decimal(1) / leverage >= Decimal("2.0") * Decimal(stop_frac)


# --------------------------------------------------------------------------- #
# Guards that keep a plan placeable
# --------------------------------------------------------------------------- #


def test_a_plan_needing_more_margin_than_the_account_holds_is_rejected(
    config: AppConfig, clock: FrozenClock
) -> None:
    """A very tight stop on a low-volatility symbol demands >10x the account."""
    engine = RiskEngine(config, clock=clock)
    decision = engine.evaluate(
        report=report(
            symbol="BTCUSDT",
            zone=("63990", "64010"),
            stop="63964",
            targets=("64100", "64300"),
        ),
        market=market(symbol="BTCUSDT", last_price="64255.1", atr_1h="60", meta=BTCUSDT),
        account=account(capital_eur="10000"),
        portfolio=portfolio(),
    )
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.INSUFFICIENT_MARGIN


def test_a_stop_further_than_half_the_entry_cannot_keep_the_liq_buffer(
    config: AppConfig, clock: FrozenClock
) -> None:
    """ATR 30 on an 82 tape: a legal 1.5 x ATR stop is 54.8% away, so even 1x
    leverage liquidates before it. Reject rather than shave the buffer rule."""
    engine = RiskEngine(config, clock=clock)
    decision = engine.evaluate(
        report=report(zone=("82.00", "82.20"), stop="37.10", targets=("149.60", "200.00")),
        market=market(atr_1h="30"),
        account=account(),
        portfolio=portfolio(),
    )
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.LIQ_BUFFER


def test_a_missing_fx_rate_stops_the_gate_before_it_sizes(
    config: AppConfig, clock: FrozenClock
) -> None:
    """§4 converts EUR to USDT; without a rate there is nothing honest to show."""
    engine = RiskEngine(config, clock=clock)
    decision = engine.evaluate(
        report=report(),
        market=market(),
        account=account(eurusd_rate="0"),
        portfolio=portfolio(),
    )
    assert decision.reason is RejectionReason.FX_UNAVAILABLE


def test_actual_risk_never_exceeds_the_budget_after_rounding(
    config: AppConfig, clock: FrozenClock
) -> None:
    engine = RiskEngine(config, clock=clock)
    decision = engine.evaluate(
        report=report(),
        market=market(),
        account=account(),
        portfolio=portfolio(),
    )
    assert decision.plan is not None
    plan = decision.plan
    assert plan.risk_eur <= plan.planned_risk_eur
    # Rounding down can only ever lose a fraction of one qty step per rung.
    tolerance = sum(
        ((SOL_STEP * abs(entry.price - plan.stop)) / plan.eurusd_rate for entry in plan.entries),
        Decimal(0),
    )
    assert plan.planned_risk_eur - plan.risk_eur <= tolerance


SOL_STEP = Decimal("0.01")
