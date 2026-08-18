"""§3 ladder construction and §4's min-notional collapse (test plan §8.4).

The collapse path only bites on small accounts: with €10k of capital a rung is
~€1.5k, far above any exchange minimum. So the boundary cases below use small
capital deliberately — that is the regime the rule exists for.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.analyst.models import Direction, EntryZone
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.engine import RiskEngine
from sentinel.risk.ladder import build_ladder, collapse, effective_min_notional
from sentinel.risk.models import GateStatus, LadderRung, RejectionReason
from sentinel.risk.sizing import size_ladder

from .conftest import BTCUSDT, SOLUSDT, account, market, portfolio, report

ATR = Decimal("0.90")
TICK = Decimal("0.01")


def zone(low: str, high: str) -> EntryZone:
    return EntryZone(low=Decimal(low), high=Decimal(high))


def ladder(
    z: EntryZone,
    *,
    last: str = "83.40",
    direction: Direction = Direction.LONG,
    atr: Decimal = ATR,
) -> tuple[LadderRung, ...]:
    return build_ladder(
        zone=z,
        direction=direction,
        last_price=Decimal(last),
        atr=atr,
        tick_size=TICK,
        single_entry_atr_threshold=Decimal("0.5"),
        weights_pct=(Decimal("40"), Decimal("35"), Decimal("25")),
    )


# --------------------------------------------------------------------------- #
# §3 — single entry vs 3-rung ladder
# --------------------------------------------------------------------------- #


def test_narrow_zone_collapses_to_a_single_midpoint_entry() -> None:
    rungs = ladder(zone("82.00", "82.20"))  # width 0.20 < 0.45
    assert rungs == (LadderRung(price=Decimal("82.10"), weight_pct=Decimal("100")),)


def test_zone_exactly_at_the_threshold_builds_the_full_ladder() -> None:
    """§3 says '< 0.5 x ATR' is single and '>= 0.5 x ATR' is a ladder."""
    rungs = ladder(zone("82.00", "82.45"))  # width 0.45 == 0.5 x ATR
    assert len(rungs) == 3


def test_zone_one_tick_under_the_threshold_stays_single() -> None:
    rungs = ladder(zone("82.00", "82.44"))  # width 0.44 < 0.45
    assert len(rungs) == 1


def test_long_ladder_runs_from_the_nearest_edge_to_the_best_price() -> None:
    rungs = ladder(zone("82.10", "83.10"), last="83.40")
    assert rungs == (
        LadderRung(price=Decimal("83.10"), weight_pct=Decimal("40")),  # nearest price
        LadderRung(price=Decimal("82.60"), weight_pct=Decimal("35")),  # midpoint
        LadderRung(price=Decimal("82.10"), weight_pct=Decimal("25")),  # best price
    )


def test_short_ladder_mirrors_it() -> None:
    rungs = ladder(zone("83.60", "84.60"), last="83.40", direction=Direction.SHORT)
    assert rungs == (
        LadderRung(price=Decimal("83.60"), weight_pct=Decimal("40")),  # nearest price
        LadderRung(price=Decimal("84.10"), weight_pct=Decimal("35")),
        LadderRung(price=Decimal("84.60"), weight_pct=Decimal("25")),  # best price
    )


def test_rung_one_is_the_edge_nearest_price_even_when_price_sits_below_a_long_zone() -> None:
    """Price under a long zone is unusual but must not produce an undefined ladder."""
    rungs = ladder(zone("82.10", "83.10"), last="81.90")
    assert rungs[0].price == Decimal("82.10")
    assert rungs[-1].price == Decimal("83.10")


def test_prices_land_on_the_tick_grid() -> None:
    rungs = ladder(zone("82.115", "83.107"))
    assert all(r.price % TICK == 0 for r in rungs)


def test_weights_always_sum_to_one_hundred() -> None:
    for z in (zone("82.00", "82.20"), zone("82.10", "83.10")):
        assert sum(r.weight_pct for r in ladder(z)) == Decimal("100")


# --------------------------------------------------------------------------- #
# §4 — collapse, owner rule: drop the far rung, renormalize the survivors
# --------------------------------------------------------------------------- #


def test_collapse_three_to_two_drops_the_far_rung_and_renormalizes() -> None:
    rungs = ladder(zone("82.10", "83.10"))
    collapsed = collapse(rungs, zone=zone("82.10", "83.10"), tick_size=TICK)
    assert collapsed == (
        LadderRung(price=Decimal("83.10"), weight_pct=Decimal("53.33")),
        LadderRung(price=Decimal("82.60"), weight_pct=Decimal("46.67")),
    )
    assert sum(r.weight_pct for r in collapsed) == Decimal("100")


def test_collapse_two_to_one_lands_on_the_zone_midpoint() -> None:
    two = (
        LadderRung(price=Decimal("83.10"), weight_pct=Decimal("53.33")),
        LadderRung(price=Decimal("82.60"), weight_pct=Decimal("46.67")),
    )
    collapsed = collapse(two, zone=zone("82.10", "83.10"), tick_size=TICK)
    assert collapsed == (LadderRung(price=Decimal("82.60"), weight_pct=Decimal("100")),)


def test_a_single_rung_cannot_collapse_further() -> None:
    one = (LadderRung(price=Decimal("82.60"), weight_pct=Decimal("100")),)
    assert collapse(one, zone=zone("82.10", "83.10"), tick_size=TICK) is None


# --------------------------------------------------------------------------- #
# §4 correction — the floor is max(exchange_minimum, 20), never a fixed 20
# --------------------------------------------------------------------------- #


def test_effective_minimum_is_the_exchange_rule_when_it_is_higher() -> None:
    assert effective_min_notional(BTCUSDT, Decimal("20")) == Decimal("50")


def test_effective_minimum_is_the_practicality_floor_when_the_exchange_is_lower() -> None:
    assert effective_min_notional(SOLUSDT, Decimal("20")) == Decimal("20")


@pytest.mark.parametrize(
    ("risk_usdt", "expected_rungs"),
    [("1.1875", 3), ("1.18", 2)],
)
def test_collapse_triggers_exactly_at_the_minimum(risk_usdt: str, expected_rungs: int) -> None:
    """The boundary is the **rounded** rung, because that is what the exchange sees.

    Under risk weighting rung 1 is the smallest rung: it is nearest price and so
    furthest from the stop. At 83.10 with a 1.90 stop distance and a 0.01 qty
    step, a risk budget of 1.1875 USDT gives it 0.40 x 1.1875 / 1.90 = 0.25 exactly
    → 20.775 USDT, clearing the 20 floor. 1.18 gives 0.2484 → floors to 0.24 →
    19.944 USDT, which does not.
    """
    sized = size_ladder(
        rungs=ladder(zone("82.10", "83.10")),
        zone=zone("82.10", "83.10"),
        stop=Decimal("81.20"),
        direction=Direction.LONG,
        risk_usdt=Decimal(risk_usdt),
        instrument=SOLUSDT,
        min_rung_notional_usdt=Decimal("20"),
        eurusd_rate=Decimal("1"),
    )
    assert sized is not None
    assert len(sized.rungs) == expected_rungs


# --------------------------------------------------------------------------- #
# End to end through the gate
# --------------------------------------------------------------------------- #


def test_small_account_collapses_to_two_rungs(config: AppConfig, clock: FrozenClock) -> None:
    """€120 of capital puts rung 1 at ~17.5 USDT — under SOLUSDT's effective 20.

    Targets carry RR headroom on purpose: collapsing drops the best-priced rung,
    which moves the weighted entry towards price and therefore *lowers* RR. A
    setup with only 1.51R to spare would (correctly) be rejected after collapsing
    — see ``test_a_collapse_that_breaks_rr_is_rejected``.
    """
    engine = RiskEngine(config, clock=clock)
    decision = engine.evaluate(
        report=report(targets=("86.00", "88.00", "90.00")),
        market=market(),
        account=account(capital_eur="120"),
        portfolio=portfolio(),
    )
    assert decision.status is GateStatus.APPROVED_FOR_HUMAN
    assert decision.plan is not None
    assert len(decision.plan.entries) == 2
    assert all(e.notional_usdt >= Decimal("20") for e in decision.plan.entries)


def test_a_collapse_that_breaks_rr_is_rejected(config: AppConfig, clock: FrozenClock) -> None:
    """§2 must hold for the plan that *ships*, not the one first drafted.

    The baseline setup has 1.51R at TP1 on three rungs. Collapsing to two moves
    the weighted entry from 82.675 to 82.867, so the real RR becomes 1.22 — below
    the 1.5 minimum, and the gate says so rather than shipping it.
    """
    engine = RiskEngine(config, clock=clock)
    decision = engine.evaluate(
        report=report(),
        market=market(),
        account=account(capital_eur="120"),
        portfolio=portfolio(),
    )
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.RR_TOO_LOW
    assert "collapsing to 2 rung(s)" in decision.message


def test_account_too_small_for_even_one_rung_is_rejected(
    config: AppConfig, clock: FrozenClock
) -> None:
    engine = RiskEngine(config, clock=clock)
    decision = engine.evaluate(
        report=report(),
        market=market(),
        account=account(capital_eur="30"),
        portfolio=portfolio(),
    )
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.MIN_NOTIONAL


def test_btcusdt_uses_its_own_higher_minimum(config: AppConfig, clock: FrozenClock) -> None:
    """The same account size that keeps 3 rungs on SOLUSDT collapses on BTCUSDT,
    because Binance's minimum there is 50 USDT, not the 20 USDT floor."""
    engine = RiskEngine(config, clock=clock)
    btc_report = report(
        symbol="BTCUSDT",
        zone=("63900", "64100"),
        stop="63800",
        targets=("64600", "65000", "65400"),
    )
    btc_market = market(symbol="BTCUSDT", last_price="64255.1", atr_1h="90", meta=BTCUSDT)
    small = engine.evaluate(
        report=btc_report,
        market=btc_market,
        account=account(capital_eur="70"),
        portfolio=portfolio(),
    )
    assert small.status is GateStatus.APPROVED_FOR_HUMAN
    assert small.plan is not None
    assert len(small.plan.entries) < 3
    assert all(e.notional_usdt >= Decimal("50") for e in small.plan.entries)

    large = engine.evaluate(
        report=btc_report,
        market=btc_market,
        account=account(capital_eur="10000"),
        portfolio=portfolio(),
    )
    assert large.plan is not None
    assert len(large.plan.entries) == 3
