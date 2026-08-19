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

**Costs (§4.2, correction 2026-08-18).** Every case below now also states its fee
arithmetic and its NET RR, because ``min_rr_tp1`` gates on the net figure. Entries
are maker (0.02%), every exit taker (0.05%). The baseline market carries no funding
rate, so these goldens isolate fees; funding has its own tests.

    net_rr_i = (rr_i x risk_eur - entry_fee - tp_exit_fee_i)
               / (risk_eur + entry_fee + stop_exit_fee)

Each case's TP1 was moved out from its M4 value, which sat at 1.50-1.51 gross and
lands below 1.50 net. Those M4 targets are kept as explicit rejection goldens at
the bottom of this file — they are the clearest statement of what changed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.analyst.models import AnalystReport, Direction, TimeframeLabel
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import GateStatus, RejectionReason, TradePlan

from .conftest import account, margin_budget, market, portfolio, report


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
    RR gross   2.525/1.475 = 1.71 · 3.925/1.475 = 2.66 · 6.225/1.475 = 4.22
    fees       Q = 18.30+21.73+24.15 = 64.18
               entry  4570.30 x 0.02%                      = €0.91
               stop   64.18(81.20)/1.1593 x 0.05% = 4495.42 x 0.05% = €2.25
               TP1    64.18(85.20)/1.1593 x 0.05% = 4716.87 x 0.05% = €2.36
               TP2 €2.40 · TP3 €2.46 (same form)
               round trip = 0.9141 + 2.2477 = €3.16 = 3.16/75.00 = 4.2133%
    RR net     TP1 (1.71x74.98 - 0.91 - 2.36)/(74.98 + 0.91 + 2.25)
               = (128.22 - 3.27)/78.14 = 124.95/78.14 = 1.60
               TP2 (199.45 - 3.31)/78.14 = 2.51 · TP3 (316.42 - 3.37)/78.14 = 4.01
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
    assert plan.stop_distance_pct == Decimal("1.78")
    assert plan.notional_usdt == Decimal("5298.343")
    assert plan.notional_eur == Decimal("4570.30")
    assert plan.planned_risk_eur == Decimal("75.00")
    assert plan.risk_eur == Decimal("74.98")
    assert plan.suggested_leverage == 5
    assert plan.margin_eur == Decimal("914.06")
    assert plan.liq_distance_pct == Decimal("20")
    assert plan.liq_buffer_ok is True
    assert plan.rr_targets == (Decimal("1.71"), Decimal("2.66"), Decimal("4.22"))
    assert plan.rr_targets_net == (Decimal("1.60"), Decimal("2.51"), Decimal("4.01"))

    costs = plan.costs
    assert costs.entry_fee_eur == Decimal("0.91")
    assert costs.stop_exit_fee_eur == Decimal("2.25")
    assert costs.tp_exit_fees_eur == (Decimal("2.36"), Decimal("2.40"), Decimal("2.46"))
    assert costs.round_trip_cost_eur == Decimal("3.16")
    assert costs.cost_pct_of_risk == Decimal("4.21")
    # No funding rate in the baseline market → estimated, absent, and labelled so.
    assert costs.funding_available is False
    assert costs.funding_eur == Decimal("0.00")

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
        RR gross   1.10/0.54 = 2.04 · 1.40/0.54 = 2.59 · 1.90/0.54 = 3.52
        fees       entry 11402.50 x 0.02% = €2.28
                   stop  161.01(81.56)/1.1593 x 0.05% = €5.66
                   TP1   161.01(83.20)/1.1593 x 0.05% = €5.78
                   round trip = 2.2805 + 5.6608 = €7.94 = 7.94/75.00 = 10.5867%
        RR net     TP1 (2.04x75.00 - 2.28 - 5.78)/(75.00 + 2.28 + 5.66)
                   = 144.94/82.94 = 1.75 · TP2 2.24 · TP3 3.09

    A single rung sizes identically under either reading of §4 — a risk share and
    a notional share coincide when there is only one rung.

    This is where costs bite hardest, and it is the M5 ETHUSDT shape: a 0.66% stop
    buys €11,402 of notional for a €75 risk budget, so the round trip is **10.6%**
    of the budget against 4.2% on the wide-stop case above. The old TP1 (82.91,
    1.50 gross) lands at **1.26 net** — see the rejection goldens below.
    """
    rep = report(zone=("82.00", "82.20"), stop="81.56", targets=("83.20", "83.50", "84.00"))

    # M8.2: this golden IS the shape the hard margin budget now refuses — a 0.66%
    # stop against 0.75% of risk buys 114% of capital in notional, and the numbers
    # below say so themselves (€11,402.50 notional, €1,140.25 margin, €1,000 budget).
    # The arithmetic is kept exactly as M4 hand-calculated it by opting out of the
    # budget; that the default config rejects it is asserted rather than lost.
    refused = RiskEngine(config, clock=clock).evaluate(
        report=rep, market=market(), account=account(), portfolio=portfolio()
    )
    assert refused.reason is RejectionReason.MARGIN_BUDGET_EXCEEDED

    plan = plan_for(margin_budget(config, "12"), clock, rep)

    assert len(plan.entries) == 1
    assert plan.entries[0].price == Decimal("82.10")
    assert plan.entries[0].weight_pct == Decimal("100")
    assert plan.entries[0].qty == Decimal("161.01")
    assert plan.avg_entry == Decimal("82.10")
    assert plan.stop_distance_pct == Decimal("0.66")
    assert plan.notional_eur == Decimal("11402.50")
    assert plan.risk_eur == Decimal("75.00")
    assert plan.suggested_leverage == 10  # clamped, not 12
    assert plan.margin_eur == Decimal("1140.25")
    assert plan.rr_targets == (Decimal("2.04"), Decimal("2.59"), Decimal("3.52"))
    assert plan.rr_targets_net == (Decimal("1.75"), Decimal("2.24"), Decimal("3.09"))
    assert plan.costs.entry_fee_eur == Decimal("2.28")
    assert plan.costs.stop_exit_fee_eur == Decimal("5.66")
    assert plan.costs.round_trip_cost_eur == Decimal("7.94")
    assert plan.costs.cost_pct_of_risk == Decimal("10.59")


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
    RR gross   3.425/2.075 = 1.65 · 4.825/2.075 = 2.33 · 6.525/2.075 = 3.14
    fees       Q = 13.91+15.21+14.49 = 43.61
               entry 3163.88 x 0.02% = €0.63
               stop  43.61(86.10)/1.1593 x 0.05% = €1.62
               TP1   43.61(80.60)/1.1593 x 0.05% = €1.52
               round trip = 0.6328 + 1.6205 = €2.25 = 2.25/75.00 = 3.0000%
    RR net     TP1 (1.65x74.98 - 0.63 - 1.52)/(74.98 + 0.63 + 1.62)
               = 121.57/77.23 = 1.57 · TP2 2.23 · TP3 3.02

    The widest stop of the four (2.31 x ATR), so the smallest notional per euro of
    risk and the lightest cost load: 3.0% of budget against the single-entry 10.6%.
    """
    plan = plan_for(
        config,
        clock,
        report(
            direction=Direction.SHORT,
            zone=("83.60", "84.60"),
            stop="86.10",
            targets=("80.60", "79.20", "77.50"),
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
    assert plan.stop_distance_pct == Decimal("2.47")
    assert plan.notional_eur == Decimal("3163.88")
    assert plan.risk_eur == Decimal("74.98")
    assert plan.suggested_leverage == 4
    assert plan.margin_eur == Decimal("790.97")
    assert plan.rr_targets == (Decimal("1.65"), Decimal("2.33"), Decimal("3.14"))
    assert plan.rr_targets_net == (Decimal("1.57"), Decimal("2.23"), Decimal("3.02"))
    assert plan.costs.entry_fee_eur == Decimal("0.63")
    assert plan.costs.stop_exit_fee_eur == Decimal("1.62")
    assert plan.costs.round_trip_cost_eur == Decimal("2.25")
    assert plan.costs.cost_pct_of_risk == Decimal("3.0000")
    # swing → 36h TTL (§5)
    assert plan.expires_at == datetime(2026, 8, 20, 0, 0, tzinfo=UTC)


def test_short_single_entry(config: AppConfig, clock: FrozenClock) -> None:
    """Zone 84.00-84.20 → one entry at 84.10, stop 84.64 (0.6 x ATR above).

    qty        86.9475 / 0.54 = 161.01389 → 161.01
    notional   161.01 x 84.10 = 13,540.941 USDT = €11,680.27
    risk       161.01 x 0.54 = 86.9454 USDT = €75.00
    leverage   11.680 → ceil 12 → clamped 10x ; margin €1,168.03
    RR gross   1.10/0.54 = 2.04 · 1.60/0.54 = 2.96
    fees       entry 11680.27 x 0.02% = €2.34
               stop  161.01(84.64)/1.1593 x 0.05% = €5.88
               TP1   161.01(83.00)/1.1593 x 0.05% = €5.76
               round trip = 2.3361 + 5.8752 = €8.21 = 8.21/75.00 = 10.9467%
    RR net     TP1 (2.04x75.00 - 2.34 - 5.76)/(75.00 + 2.34 + 5.88)
               = 144.90/83.22 = 1.74 · TP2 2.57
    """
    rep = report(
        direction=Direction.SHORT,
        zone=("84.00", "84.20"),
        stop="84.64",
        targets=("83.00", "82.50"),
    )

    # M8.2, mirroring the long case: a 0.64% stop against 0.75% of risk buys 117%
    # of capital, so the default config now refuses this shape. See `margin_budget`.
    refused = RiskEngine(config, clock=clock).evaluate(
        report=rep, market=market(), account=account(), portfolio=portfolio()
    )
    assert refused.reason is RejectionReason.MARGIN_BUDGET_EXCEEDED

    plan = plan_for(margin_budget(config, "12"), clock, rep)

    assert len(plan.entries) == 1
    assert plan.entries[0].qty == Decimal("161.01")
    assert plan.avg_entry == Decimal("84.10")
    assert plan.notional_eur == Decimal("11680.27")
    assert plan.risk_eur == Decimal("75.00")
    assert plan.suggested_leverage == 10
    assert plan.margin_eur == Decimal("1168.03")
    assert plan.rr_targets == (Decimal("2.04"), Decimal("2.96"))
    assert plan.rr_targets_net == (Decimal("1.74"), Decimal("2.57"))
    assert plan.costs.entry_fee_eur == Decimal("2.34")
    assert plan.costs.stop_exit_fee_eur == Decimal("5.88")
    assert plan.costs.round_trip_cost_eur == Decimal("8.21")


# --------------------------------------------------------------------------- #
# §4.2 — the M4 targets, kept as rejection goldens
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "rep", "gross", "net"),
    [
        (
            "long 3-rung",
            report(targets=("84.90", "86.60", "88.90")),
            Decimal("1.51"),
            Decimal("1.41"),
        ),
        (
            "long single-entry",
            report(zone=("82.00", "82.20"), stop="81.56", targets=("82.91", "83.50", "84.00")),
            Decimal("1.50"),
            Decimal("1.26"),
        ),
        (
            "short 3-rung",
            report(
                direction=Direction.SHORT,
                zone=("83.60", "84.60"),
                stop="86.10",
                targets=("80.90", "79.20", "77.50"),
                timeframe_label=TimeframeLabel.SWING,
            ),
            Decimal("1.51"),
            Decimal("1.44"),
        ),
        (
            "short single-entry",
            report(
                direction=Direction.SHORT,
                zone=("84.00", "84.20"),
                stop="84.64",
                targets=("83.29", "82.50"),
            ),
            Decimal("1.50"),
            Decimal("1.25"),
        ),
    ],
    ids=["long_3rung", "long_single", "short_3rung", "short_single"],
)
def test_the_m4_goldens_are_now_rejected_on_net_rr(
    config: AppConfig,
    clock: FrozenClock,
    name: str,
    rep: AnalystReport,
    gross: Decimal,
    net: Decimal,
) -> None:
    """The four §8.1 goldens as M4 shipped them: 1.50-1.51 gross, all below 1.50 net.

    This is the behaviour change stated as a test rather than left to be
    discovered during M9. ``min_rr_tp1`` is unchanged at 1.5, but it now measures
    a different quantity, so these four setups — approved through M4 and M5 — are
    rejected. The loss is largest where the stop is tightest: the single-entry
    cases give up 0.24-0.25R to costs, the wide-stop short only 0.07R.
    """
    # The subject here is §4.2's *net* RR, which the engine evaluates last — after
    # the margin checks. The two single-entry shapes are also the ones M8.2's hard
    # margin budget now stops earlier, so the budget is widened to let the net-RR
    # rail be the one under test. `test_margin_budget.py` asserts the other order.
    decision = RiskEngine(margin_budget(config, "12"), clock=clock).evaluate(
        report=rep, market=market(), account=account(), portfolio=portfolio()
    )

    assert decision.status is GateStatus.REJECTED, name
    assert decision.reason is RejectionReason.NET_RR_TOO_LOW, name
    assert decision.plan is None
    # The message names both figures — a rejection the owner cannot audit is noise.
    assert f"{net}R net" in decision.message
    assert f"{gross}R gross" in decision.message


def test_management_plan_is_the_deterministic_template(
    config: AppConfig, clock: FrozenClock
) -> None:
    """§5 — percentages come from config, never from the LLM."""
    plan = plan_for(config, clock, report())
    assert plan.management_plan == (
        "TP1: close 40%, move stop to breakeven. TP2: close 35%. "
        "TP3: close remainder or trail by 1xATR."
    )
