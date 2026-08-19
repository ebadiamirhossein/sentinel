"""``margin_budget_pct`` as a hard limit (M8.2, owner ruling 2026-08-19).

**This is a deliberate change to specs/RISK_ENGINE.md §4, not a bug fix.** §4 as
written calls `margin_budget_pct` a *"Target margin per trade as share of capital"*
and then says, in the sizing block, ``margin_eur_final = notional_eur / leverage
# recomputed after clamping``. So the budget only ever *derived* a starting
leverage; the clamp to ``max_leverage`` silently discarded it, and the recomputed
margin could land anywhere.

The M8.2 diagnostic found two of seven live candidate plans sizing to **105% and
114% of total capital** in notional, needing €21.00 and €22.76 of margin against a
€20.00 budget — and being approved. The owner's ruling: *"A '10% margin budget'
that permits that means nothing."*

**The rail is capital-independent, and that is worth stating because it is easy to
get wrong.** ``margin / capital == risk_pct / (stop_fraction x leverage)`` — both
notional and budget scale linearly with capital, so €200 and €10,000 behave
identically. With ``max_leverage`` 10 and a 10% budget the rail fires exactly when
notional exceeds 100% of capital, i.e. when ``stop_fraction < risk_per_trade_pct``.
It is **tight stops** that trigger it, not small accounts. The reason it never fired
before is simply that every golden in the suite uses the 81.20 stop — 1.78% away,
against 0.75% of risk — which sizes to 42% of capital and 4.2% of margin. The live
plans that broke it had stops 0.69% and 0.75% away.

So the budget now rejects. Two consequences are asserted here rather than left to
be rediscovered:

* **Order matters.** ``margin > capital`` is checked *first*. It is the more serious
  finding — the owner cannot fund the position at all — and it is strictly stronger
  than exceeding a budget that is 10% of the same capital. Checking the budget first
  would make ``INSUFFICIENT_MARGIN`` unreachable, i.e. a dead branch in a module that
  is required to hold 100% branch coverage.
* **The buffer runs before the check.** The liquidation-buffer rule *reduces*
  leverage, which *raises* margin. A budget checked before it would pass plans the
  buffer then pushes over.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import GateDecision, GateStatus, RejectionReason

from .conftest import account, market, portfolio, report

#: A zone narrower than ``0.5 x ATR(1h)`` (0.20 against 0.45), so §3 builds a
#: **single** rung at the midpoint 83.00. Deliberate: a three-rung ladder whose far
#: rung sits a tick above the stop needs enormous size for its share of the risk,
#: which blows notional up for reasons that have nothing to do with the budget.
NARROW: tuple[str, str] = ("82.90", "83.10")


def evaluate(
    config: AppConfig,
    clock: FrozenClock,
    *,
    capital_eur: str = "10000",
    stop: str = "81.20",
    zone: tuple[str, str] = ("82.10", "83.10"),
    targets: tuple[str, ...] = ("85.20", "86.60", "88.90"),
) -> GateDecision:
    return RiskEngine(config, clock=clock).evaluate(
        report=report(zone=zone, stop=stop, targets=targets),
        market=market(),
        account=account(capital_eur=capital_eur),
        portfolio=portfolio(),
    )


# --------------------------------------------------------------------------- #
# The rail
# --------------------------------------------------------------------------- #


def test_a_plan_inside_the_budget_is_unaffected(config: AppConfig, clock: FrozenClock) -> None:
    """The M4/M5 baseline still approves, and its margin still fits the budget.

    The regression that matters most: this change must not quietly re-gate the
    plans the whole suite is calibrated on.
    """
    decision = evaluate(config, clock)
    assert decision.status is GateStatus.APPROVED_FOR_HUMAN
    plan = decision.plan
    assert plan is not None
    budget = Decimal("10000") * config.risk.margin_budget_pct / Decimal(100)
    assert plan.margin_eur <= budget


def test_margin_over_the_budget_is_rejected(config: AppConfig, clock: FrozenClock) -> None:
    """The shape of the two real cases, hand-checked.

    Single rung at 83.00, stop 82.40 — 0.60 away, which is 0.667x ATR(0.90) and so
    legal under §2 rules 3 and 4, and **0.7229%** of entry. Risk is fixed while
    notional scales as ``1/stop_distance``, so::

        notional / capital = 0.75% / 0.7229% = 1.0375   -> 103.75% of capital
        leverage           = min(ceil(1.0375 / 0.10), 10) = min(11, 10) = 10
        margin  / capital  = 1.0375 / 10 = 10.375%      -> over the 10% budget

    The clamp at ``max_leverage`` is exactly where the budget used to be discarded:
    11x would have met it, 10x is the most the rail allows, and §4 as written then
    recomputed margin and shipped it anyway.
    """
    decision = evaluate(
        config, clock, capital_eur="200", zone=NARROW, stop="82.40", targets=("84.20",)
    )
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.MARGIN_BUDGET_EXCEEDED


def test_the_rejection_says_the_two_numbers(config: AppConfig, clock: FrozenClock) -> None:
    """A rail the owner will meet often at small capital has to explain itself.

    ``MARGIN_BUDGET_EXCEEDED`` on its own reads as a configuration error. The message
    carries what was needed against what was allowed, so the owner can tell "raise the
    budget" from "this setup is too big for this account".
    """
    decision = evaluate(
        config, clock, capital_eur="200", zone=NARROW, stop="82.40", targets=("84.20",)
    )
    assert "€" in decision.message
    assert "20" in decision.message  # the budget
    assert decision.reason is RejectionReason.MARGIN_BUDGET_EXCEEDED


def test_insufficient_margin_still_wins_over_the_budget(
    config: AppConfig, clock: FrozenClock
) -> None:
    """The harder blocker is reported, and stays reachable.

    Margin above *capital* implies margin above a budget that is 10% of it, so if the
    budget were checked first ``INSUFFICIENT_MARGIN`` could never be returned again —
    a dead branch under a 100%-branch-coverage requirement, and a worse message for
    the owner. Order is asserted, not assumed.
    """
    decision = RiskEngine(config, clock=clock).evaluate(
        report=report(zone=("82.99", "83.01"), stop="82.96", targets=("84.20",)),
        market=market(atr_1h="0.05"),
        account=account(capital_eur="200"),
        portfolio=portfolio(),
    )
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.INSUFFICIENT_MARGIN


@pytest.mark.parametrize("budget_pct", ["10", "25", "100"])
def test_a_wider_budget_admits_what_a_narrow_one_refused(
    config: AppConfig, clock: FrozenClock, budget_pct: str
) -> None:
    """The rail is the knob it claims to be — monotonic in ``margin_budget_pct``.

    Guards against a fix that hardcodes the 10% rather than reading the setting.
    """
    widened = config.model_copy(
        update={"risk": config.risk.model_copy(update={"margin_budget_pct": Decimal(budget_pct)})}
    )
    decision = evaluate(
        widened, clock, capital_eur="200", zone=NARROW, stop="82.40", targets=("84.20",)
    )
    if budget_pct == "10":
        assert decision.reason is RejectionReason.MARGIN_BUDGET_EXCEEDED
    else:
        assert decision.reason is not RejectionReason.MARGIN_BUDGET_EXCEEDED


def test_an_approved_plan_never_exceeds_its_budget(config: AppConfig, clock: FrozenClock) -> None:
    """The invariant, over the whole capital range the owner might set.

    Stated as a sweep rather than one example because this is the property the rail
    exists to guarantee: if a plan ships, its margin fits. €200 is today's capital and
    €10,000 was yesterday's; the middle is where a fix that special-cases one end
    would show.
    """
    for capital in ("200", "500", "1000", "2500", "10000"):
        decision = evaluate(config, clock, capital_eur=capital)
        if decision.status is not GateStatus.APPROVED_FOR_HUMAN:
            continue
        plan = decision.plan
        assert plan is not None
        budget = Decimal(capital) * config.risk.margin_budget_pct / Decimal(100)
        assert plan.margin_eur <= budget, f"€{capital} shipped {plan.margin_eur} over {budget}"


def test_the_budget_is_checked_after_the_liquidation_buffer(
    config: AppConfig, clock: FrozenClock
) -> None:
    """The buffer lowers leverage, which raises margin — so it cannot run afterwards.

    With ``liq_buffer_multiple`` raised, leverage is forced down and the same plan
    needs strictly more margin. If the budget were evaluated against the pre-buffer
    leverage, this plan would pass on a number the owner never gets.

    A stop 0.9036% away sizes to 83% of capital and needs 9x, i.e. 9.22% of capital
    in margin — comfortably inside the budget. Raise ``liq_buffer_multiple`` to 14
    and the buffer caps leverage at ``int(1 / (14 x 0.009036)) = 7``, which lifts
    margin to ``0.83 / 7 = 11.86%`` and over the line. Both halves are asserted, so
    the test fails if the plan was rejected for some unrelated reason.
    """
    passes = evaluate(
        config, clock, capital_eur="200", zone=NARROW, stop="82.25", targets=("86.00",)
    )
    assert passes.status is GateStatus.APPROVED_FOR_HUMAN, (
        "the control must approve, or the buffer is not what this test is measuring"
    )

    strict = config.model_copy(
        update={"risk": config.risk.model_copy(update={"liq_buffer_multiple": Decimal("14.0")})}
    )
    decision = evaluate(
        strict, clock, capital_eur="200", zone=NARROW, stop="82.25", targets=("86.00",)
    )
    assert decision.status is GateStatus.REJECTED
    assert decision.reason is RejectionReason.MARGIN_BUDGET_EXCEEDED
    assert decision.plan is None
