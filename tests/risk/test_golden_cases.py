"""§8.1 golden cases — long/short x single-entry/3-rung, hand-calculated.

Every expected number below was computed by hand from specs/RISK_ENGINE.md §3-§4
(the arithmetic is written out in each docstring) — not copied from the engine's
own output. A golden that a program generated from the code it tests only proves
the code agrees with itself.

Shared inputs: capital €10,000 · risk 0.75% (= €75 = 86.9475 USDT at 1.1593) ·
ATR(14,1h) 0.90 · last price 83.40 · SOLUSDT (tick 0.01, step 0.01, exchange
minimum 5 → the engine's floor is max(5, 20) = 20).

Rung weights are shares of the **risk budget** (owner ruling, §3/§8.5 over §4):
``qty_i = risk_usdt x w_i / |p_i - stop|``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sentinel.analyst.models import AnalystReport, Direction, TimeframeLabel
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import GateStatus, TradePlan

from .conftest import account, market, portfolio, report


def plan_for(config: AppConfig, clock: FrozenClock, rep: AnalystReport) -> TradePlan:
    decision = RiskEngine(config, clock=clock).evaluate(
        report=rep, market=market(), account=account(), portfolio=portfolio()
    )
    assert decision.status is GateStatus.APPROVED_FOR_HUMAN, decision.message
    assert decision.plan is not None
    return decision.plan


def test_long_three_rung_ladder(config: AppConfig, clock: FrozenClock) -> None:
    """SOLUSDT long, zone 82.10-83.10 (width 1.00 >= 0.5 x ATR) → 3 rungs.

    rungs      83.10 @40% · 82.60 @35% · 82.10 @25%
    E          0.4(83.10)+0.35(82.60)+0.25(82.10) = 82.675   (§3)
    stop dist  82.675 - 81.20 = 1.475  (= 1.639 x ATR, inside 0.6-3.0)
               1.475 / 82.675 = 1.7841%
    risk       €10,000 x 0.75% = €75 → x 1.1593 = 86.9475 USDT
    qty        40%: 34.779   / 1.90 = 18.30474 → 18.30  (floor to 0.01)
               35%: 30.431625/ 1.40 = 21.73688 → 21.73
               25%: 21.736875/ 0.90 = 24.15208 → 24.15
    notional   18.30(83.10)+21.73(82.60)+24.15(82.10)
               = 1520.730 + 1794.898 + 1982.715 = 5,298.343 USDT = €4,570.30
    avg fill   5298.343 / 64.18 = 82.5544 → 82.55
    actual     risk 18.30(1.90)+21.73(1.40)+24.15(0.90)
               = 34.770 + 30.422 + 21.735 = 86.927 USDT = €74.98
               rung 1 share = 34.770/86.927 = 40.0% → §3's -0.40R holds
    leverage   4570.30 / €1,000 margin budget = 4.570 → ceil 5x
               liq ≈ 1/5 = 20% ≥ 2 x 1.7841% ✓ → margin 4570.30/5 = €914.06
    RR         2.225/1.475 = 1.51 · 3.925/1.475 = 2.66 · 6.225/1.475 = 4.22
    """
    plan = plan_for(config, clock, report())

    assert [(e.price, e.weight_pct, e.qty) for e in plan.entries] == [
        (Decimal("83.10"), Decimal("40"), Decimal("18.30")),
        (Decimal("82.60"), Decimal("35"), Decimal("21.73")),
        (Decimal("82.10"), Decimal("25"), Decimal("24.15")),
    ]
    assert plan.avg_entry == Decimal("82.675")
    assert plan.avg_fill_price == Decimal("82.55")
    assert plan.stop == Decimal("81.20")
    assert plan.stop_distance_pct == Decimal("1.7841")
    assert plan.notional_usdt == Decimal("5298.343")
    assert plan.notional_eur == Decimal("4570.30")
    assert plan.planned_risk_eur == Decimal("75.00")
    assert plan.risk_eur == Decimal("74.98")
    assert plan.suggested_leverage == 5
    assert plan.margin_eur == Decimal("914.06")
    assert plan.liq_distance_pct == Decimal("20")
    assert plan.liq_buffer_ok is True
    assert plan.rr_targets == (Decimal("1.51"), Decimal("2.66"), Decimal("4.22"))
    # intraday → 12h TTL (§5)
    assert plan.expires_at == datetime(2026, 8, 19, 0, 0, tzinfo=UTC)


def test_long_single_entry(config: AppConfig, clock: FrozenClock) -> None:
    """Zone 82.00-82.20 (width 0.20 < 0.45) → one entry at the midpoint 82.10.

        stop dist  0.54 = exactly 0.6 x ATR → 0.54/82.10 = 0.6577%
        qty        86.9475 / 0.54 = 161.01389 → 161.01
        notional   161.01 x 82.10 = 13,218.921 USDT = €11,402.50
        actual     risk 161.01 x 0.54 = 86.9454 USDT = €75.00
        leverage   11402.50/1000 = 11.40 → ceil 12 → clamped to max 10x
                   liq 10% ≥ 2 x 0.6577% ✓ → margin 11402.50/10 = €1,140.25
        RR         0.81/0.54 = 1.50 exactly · 1.40/0.54 = 2.59 · 1.90/0.54 = 3.52

    A single rung sizes identically under either reading of §4 — a risk share and
    a notional share coincide when there is only one rung.
    """
    plan = plan_for(
        config,
        clock,
        report(zone=("82.00", "82.20"), stop="81.56", targets=("82.91", "83.50", "84.00")),
    )

    assert len(plan.entries) == 1
    assert plan.entries[0].price == Decimal("82.10")
    assert plan.entries[0].weight_pct == Decimal("100")
    assert plan.entries[0].qty == Decimal("161.01")
    assert plan.avg_entry == Decimal("82.10")
    assert plan.stop_distance_pct == Decimal("0.6577")
    assert plan.notional_eur == Decimal("11402.50")
    assert plan.risk_eur == Decimal("75.00")
    assert plan.suggested_leverage == 10  # clamped, not 12
    assert plan.margin_eur == Decimal("1140.25")
    assert plan.rr_targets == (Decimal("1.50"), Decimal("2.59"), Decimal("3.52"))


def test_short_three_rung_ladder(config: AppConfig, clock: FrozenClock) -> None:
    """SOLUSDT short, zone 83.60-84.60, stop 86.10, swing label.

    rungs      83.60 @40% (nearest price) · 84.10 @35% · 84.60 @25%
    E          84.025 ; stop dist 2.075 (2.306 x ATR) = 2.4695%
    qty        34.779   / 2.50 = 13.9116   → 13.91
               30.431625/ 2.00 = 15.21581  → 15.21
               21.736875/ 1.50 = 14.49125  → 14.49
    notional   13.91(83.60)+15.21(84.10)+14.49(84.60)
               = 1162.876 + 1279.161 + 1225.854 = 3,667.891 USDT = €3,163.88
    avg fill   3667.891 / 43.61 = 84.1067 → 84.11
    actual     risk 13.91(2.50)+15.21(2.00)+14.49(1.50) = 86.930 USDT = €74.98
    leverage   3163.88/1000 = 3.164 → ceil 4x ; liq 25% ≥ 4.939% ✓
               margin 3163.88/4 = €790.97
    RR         3.125/2.075 = 1.51 · 4.825/2.075 = 2.33 · 6.525/2.075 = 3.14
    """
    plan = plan_for(
        config,
        clock,
        report(
            direction=Direction.SHORT,
            zone=("83.60", "84.60"),
            stop="86.10",
            targets=("80.90", "79.20", "77.50"),
            timeframe_label=TimeframeLabel.SWING,
        ),
    )

    assert [(e.price, e.weight_pct, e.qty) for e in plan.entries] == [
        (Decimal("83.60"), Decimal("40"), Decimal("13.91")),
        (Decimal("84.10"), Decimal("35"), Decimal("15.21")),
        (Decimal("84.60"), Decimal("25"), Decimal("14.49")),
    ]
    assert plan.avg_entry == Decimal("84.025")
    assert plan.avg_fill_price == Decimal("84.11")
    assert plan.stop_distance_pct == Decimal("2.4695")
    assert plan.notional_eur == Decimal("3163.88")
    assert plan.risk_eur == Decimal("74.98")
    assert plan.suggested_leverage == 4
    assert plan.margin_eur == Decimal("790.97")
    assert plan.rr_targets == (Decimal("1.51"), Decimal("2.33"), Decimal("3.14"))
    # swing → 36h TTL (§5)
    assert plan.expires_at == datetime(2026, 8, 20, 0, 0, tzinfo=UTC)


def test_short_single_entry(config: AppConfig, clock: FrozenClock) -> None:
    """Zone 84.00-84.20 → one entry at 84.10, stop 84.64 (0.6 x ATR above).

    qty        86.9475 / 0.54 = 161.01389 → 161.01
    notional   161.01 x 84.10 = 13,540.941 USDT = €11,680.27
    risk       161.01 x 0.54 = 86.9454 USDT = €75.00
    leverage   11.680 → ceil 12 → clamped 10x ; margin €1,168.03
    RR         0.81/0.54 = 1.50 · 1.60/0.54 = 2.96
    """
    plan = plan_for(
        config,
        clock,
        report(
            direction=Direction.SHORT,
            zone=("84.00", "84.20"),
            stop="84.64",
            targets=("83.29", "82.50"),
        ),
    )

    assert len(plan.entries) == 1
    assert plan.entries[0].qty == Decimal("161.01")
    assert plan.avg_entry == Decimal("84.10")
    assert plan.notional_eur == Decimal("11680.27")
    assert plan.risk_eur == Decimal("75.00")
    assert plan.suggested_leverage == 10
    assert plan.margin_eur == Decimal("1168.03")
    assert plan.rr_targets == (Decimal("1.50"), Decimal("2.96"))


def test_management_plan_is_the_deterministic_template(
    config: AppConfig, clock: FrozenClock
) -> None:
    """§5 — percentages come from config, never from the LLM."""
    plan = plan_for(config, clock, report())
    assert plan.management_plan == (
        "TP1: close 40%, move stop to breakeven. TP2: close 35%. "
        "TP3: close remainder or trail by 1xATR."
    )
