"""The gate: an ``AnalystReport`` in, a sized ``TradePlan`` or a coded rejection out.

Order of evaluation (fixed, so a rejection always names the first real problem):

1. preconditions — is this even a candidate, and do we have capital, FX,
   instrument rules and an ATR to reason with?
2. §2 rule 1 geometry, then rule 2 entry distance — both on the analyst's zone;
3. §2 rule 6 confidence — a downgrade to WATCHLIST, not a rejection;
4. §3 ladder → weighted average entry E;
5. §2 rules 3-5 — stop distance in ATR multiples, and RR to TP1, measured from E;
6. §2 rule 7 portfolio rails;
7. §4 sizing, collapsing the ladder while any rung is below the exchange minimum;
8. rules 3-5 **again** on the final ladder — a collapse moves E, and the plan that
   ships must satisfy §2, not merely the plan that was first drafted;
9. leverage, liquidation buffer and the margin guard;
10. §4.2 costs, and rule 5 once more on the **net** RR — the figure the owner
    actually collects. Costs need real quantities, so this cannot run before
    sizing; it runs after the funding checks because a position that cannot be
    funded is a harder blocker than a thin RR (see the note at the call site).

No LLM anywhere in this module (rule zero). No floats.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sentinel.analyst.models import AnalystReport, CandidateStatus, Direction, EntryZone
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import AppConfig
from sentinel.core.logging import get_logger
from sentinel.ingestion.models import InstrumentMeta
from sentinel.risk.coherence import (
    check_confidence,
    check_entry_distance,
    check_geometry,
    check_rr,
    check_stop_distance,
    rr_multiples,
)
from sentinel.risk.costs import estimate_costs, net_rr_multiples
from sentinel.risk.ladder import build_ladder
from sentinel.risk.management import expires_at, management_plan_text
from sentinel.risk.models import (
    AccountState,
    Frozen,
    GateDecision,
    GateStatus,
    MarketContext,
    PlanCosts,
    PortfolioState,
    RejectionReason,
    TradePlan,
)
from sentinel.risk.rails import check_portfolio_rails
from sentinel.risk.rounding import money, percent, ratio, round_stop_to_tick
from sentinel.risk.sizing import (
    SizedLadder,
    distance_pct,
    risk_budget_eur,
    size_ladder,
    solve_leverage,
    stop_distance_fraction,
    weighted_avg_entry,
)

log = get_logger(__name__)

HUNDRED = Decimal("100")

MESSAGES: dict[RejectionReason, str] = {
    RejectionReason.NOT_A_CANDIDATE: "analyst status is not CANDIDATE",
    RejectionReason.MISSING_PLAN_FIELDS: "report is missing direction, zone, stop or targets",
    RejectionReason.NO_CAPITAL: "capital_eur is not set — run /capital first",
    RejectionReason.ATR_UNAVAILABLE: "ATR(14,1h) unavailable — cannot check stop distance",
    RejectionReason.INSTRUMENT_META_MISSING: "no exchange rules for this symbol",
    RejectionReason.FX_UNAVAILABLE: "no EURUSD rate — cannot size in EUR",
    RejectionReason.ENTRY_ZONE_INVALID: "entry zone is not a positive ordered range",
    RejectionReason.STOP_SIDE: "stop is on the wrong side of the entry zone",
    RejectionReason.TARGET_ORDER: "targets are not ordered beyond the entry zone",
    RejectionReason.ENTRY_TOO_FAR: "entry zone is too far from the last price",
    RejectionReason.STOP_TOO_TIGHT: "stop is inside the noise band (< min x ATR)",
    RejectionReason.STOP_TOO_WIDE: "stop is wider than the ATR ceiling",
    RejectionReason.RR_TOO_LOW: "reward-to-risk at TP1 is below the minimum",
    RejectionReason.NET_RR_TOO_LOW: (
        "reward-to-risk at TP1 is below the minimum once fees and funding are paid"
    ),
    RejectionReason.LOW_CONFIDENCE: "confidence below the candidate threshold",
    RejectionReason.MAX_OPEN_RISK: "open risk budget would be exceeded",
    RejectionReason.MAX_POSITIONS: "maximum concurrent positions reached",
    RejectionReason.SYMBOL_COOLDOWN: "symbol is on cooldown",
    RejectionReason.PAUSED: "signals are paused",
    RejectionReason.MIN_NOTIONAL: "account too small: no rung clears the exchange minimum",
    RejectionReason.LIQ_BUFFER: "liquidation buffer unreachable above 1x leverage",
    RejectionReason.INSUFFICIENT_MARGIN: "required margin exceeds capital",
    RejectionReason.MARGIN_BUDGET_EXCEEDED: "required margin exceeds the margin budget",
}


class _Inputs(Frozen):
    """The precondition-checked inputs, with every ``Optional`` already resolved."""

    zone: EntryZone
    stop: Decimal
    atr: Decimal
    instrument: InstrumentMeta
    capital_eur: Decimal


class RiskEngine:
    """Deterministic risk gate. Construct once per config; ``evaluate`` is pure."""

    def __init__(self, config: AppConfig, clock: Clock | None = None) -> None:
        self._config = config
        self._clock = clock or SystemClock()

    def evaluate(
        self,
        *,
        report: AnalystReport,
        market: MarketContext,
        account: AccountState,
        portfolio: PortfolioState,
    ) -> GateDecision:
        now = self._clock.now()
        risk = self._config.risk

        checked = _check_preconditions(report, market, account)
        if isinstance(checked, RejectionReason):
            return self._decide(report, now, GateStatus.REJECTED, checked)

        geometry = check_geometry(
            direction=report.direction,
            zone=checked.zone,
            stop=checked.stop,
            targets=report.targets,
        )
        if geometry is not None:
            return self._decide(report, now, GateStatus.REJECTED, geometry)

        distance = check_entry_distance(
            zone=checked.zone,
            last_price=market.last_price,
            max_distance_pct=risk.max_entry_distance_pct,
        )
        if distance is not None:
            return self._decide(report, now, GateStatus.REJECTED, distance)

        confidence = check_confidence(
            confidence=report.confidence, min_confidence=risk.min_confidence
        )
        if confidence is not None:
            return self._decide(
                report,
                now,
                GateStatus.DOWNGRADED_WATCHLIST,
                confidence,
                detail=f"confidence {report.confidence} < {risk.min_confidence}",
            )

        stop = round_stop_to_tick(checked.stop, report.direction, checked.instrument.tick_size)
        rungs = build_ladder(
            zone=checked.zone,
            direction=report.direction,
            last_price=market.last_price,
            atr=checked.atr,
            tick_size=checked.instrument.tick_size,
            single_entry_atr_threshold=self._config.ladder.single_entry_atr_threshold,
            weights_pct=self._config.ladder.weights_pct,
        )

        drafted = self._check_quality(weighted_avg_entry(rungs), stop, checked.atr, report)
        if drafted is not None:
            return self._decide(report, now, GateStatus.REJECTED, drafted)

        rails = check_portfolio_rails(
            portfolio=portfolio,
            symbol=report.symbol,
            risk_per_trade_pct=account.risk_per_trade_pct,
            config=risk,
            now=now,
        )
        if rails is not None:
            return self._decide(report, now, GateStatus.REJECTED, rails)

        planned_risk_eur = risk_budget_eur(checked.capital_eur, account.risk_per_trade_pct)
        sized = size_ladder(
            rungs=rungs,
            zone=checked.zone,
            stop=stop,
            direction=report.direction,
            risk_usdt=planned_risk_eur * account.eurusd_rate,
            instrument=checked.instrument,
            min_rung_notional_usdt=risk.min_rung_notional_usdt,
            eurusd_rate=account.eurusd_rate,
            last_price=market.last_price,
        )
        if sized is None:
            return self._decide(report, now, GateStatus.REJECTED, RejectionReason.MIN_NOTIONAL)

        # A collapse moves E, so §2's rules are re-checked against what will ship.
        shipped = self._check_quality(sized.avg_entry, stop, checked.atr, report)
        if shipped is not None:
            return self._decide(
                report,
                now,
                GateStatus.REJECTED,
                shipped,
                detail=f"after collapsing to {len(sized.rungs)} rung(s)",
            )

        stop_fraction = stop_distance_fraction(sized.avg_entry, stop)
        leverage = solve_leverage(
            notional_eur=sized.notional_eur,
            margin_budget_eur=checked.capital_eur * risk.margin_budget_pct / HUNDRED,
            stop_distance_fraction=stop_fraction,
            max_leverage=risk.max_leverage,
            liq_buffer_multiple=risk.liq_buffer_multiple,
        )
        if leverage is None:
            return self._decide(report, now, GateStatus.REJECTED, RejectionReason.LIQ_BUFFER)

        margin_eur = money(sized.notional_eur / leverage)
        margin_budget_eur = money(checked.capital_eur * risk.margin_budget_pct / HUNDRED)
        # Order is load-bearing, and both branches must stay reachable.
        #
        # "Cannot fund it at all" is the more serious finding and is reported first.
        # It is also strictly stronger than exceeding a budget that is a *share* of
        # the same capital, so checking the budget first would make
        # INSUFFICIENT_MARGIN unreachable — a dead branch in the one module required
        # to hold 100% branch coverage, and a worse message for the owner.
        if margin_eur > checked.capital_eur:
            return self._decide(
                report, now, GateStatus.REJECTED, RejectionReason.INSUFFICIENT_MARGIN
            )
        # §4's budget as a HARD limit (owner ruling 2026-08-19; §4 called it a
        # "target" and recomputed margin after clamping to max_leverage, which
        # discarded it). This runs after solve_leverage rather than inside it
        # because the liquidation buffer *reduces* leverage and therefore *raises*
        # margin: a budget checked before the buffer would pass plans the buffer
        # then pushes over. A budget of zero means "not configured", matching
        # solve_leverage's own convention for the same value.
        if margin_budget_eur > 0 and margin_eur > margin_budget_eur:
            return self._decide(
                report,
                now,
                GateStatus.REJECTED,
                RejectionReason.MARGIN_BUDGET_EXCEEDED,
                detail=(
                    f"needs €{margin_eur} of margin at {leverage}x against a "
                    f"€{margin_budget_eur} budget ({risk.margin_budget_pct}% of capital)"
                ),
            )

        # §4.2 — cost the round trip, then re-run rule 5 on what the owner keeps.
        #
        # This runs LAST, after the funding checks, and the order is load-bearing.
        # Costs scale with notional while risk is fixed, so cost-as-a-share-of-risk
        # is ~0.07% / stop_distance — the same quantity that drives leverage. Any
        # plan failing the margin guard (notional > 10x capital) necessarily spends
        # >90% of its risk budget on fees, so gating net RR earlier would make
        # INSUFFICIENT_MARGIN and LIQ_BUFFER unreachable and report "thin RR" for
        # a position the owner simply cannot fund. The harder blocker wins.
        expiry = expires_at(
            created_at=now,
            timeframe_label=report.timeframe_label,
            config=self._config.management,
        )
        risk_eur = money(sized.risk_usdt / account.eurusd_rate)
        # Quantized before the net math, not after: the card shows 1.51R, so 1.51R
        # is what net RR must be derived from. Otherwise the two figures on the
        # same line cannot be reconciled by hand, which is how §8.1's goldens —
        # and the owner reading a card — check the engine's arithmetic.
        rr_gross = tuple(
            ratio(value)
            for value in rr_multiples(avg_entry=sized.avg_entry, stop=stop, targets=report.targets)
        )
        costs = estimate_costs(
            entries=sized.rungs,
            stop=stop,
            targets=report.targets,
            direction=report.direction,
            notional_eur=sized.notional_eur,
            planned_risk_eur=planned_risk_eur,
            eurusd_rate=account.eurusd_rate,
            funding_rate=market.funding_rate,
            next_funding_time=market.next_funding_time,
            created_at=now,
            expires_at=expiry,
            config=self._config.costs,
        )
        rr_net = net_rr_multiples(rr_gross=rr_gross, risk_eur=risk_eur, costs=costs)
        if check_rr(rr=rr_net, min_rr_tp1=risk.min_rr_tp1) is not None:
            return self._decide(
                report,
                now,
                GateStatus.REJECTED,
                RejectionReason.NET_RR_TOO_LOW,
                detail=(
                    f"{ratio(rr_net[0])}R net vs {ratio(rr_gross[0])}R gross — "
                    f"€{costs.round_trip_cost_eur} of costs on a €{risk_eur} risk"
                ),
            )

        plan = self._build_plan(
            report=report,
            checked=checked,
            sized=sized,
            stop=stop,
            stop_fraction=stop_fraction,
            leverage=leverage,
            margin_eur=margin_eur,
            planned_risk_eur=planned_risk_eur,
            risk_eur=risk_eur,
            rr_gross=rr_gross,
            rr_net=rr_net,
            costs=costs,
            expiry=expiry,
            account=account,
            market=market,
            now=now,
        )
        log.info(
            "risk.approved",
            symbol=report.symbol,
            rungs=len(plan.entries),
            risk_eur=str(plan.risk_eur),
            leverage=plan.suggested_leverage,
        )
        return GateDecision(
            symbol=report.symbol,
            status=GateStatus.APPROVED_FOR_HUMAN,
            plan=plan,
            message="approved for human review",
            evaluated_at=now,
            prompt_version=report.prompt_version,
        )

    # ------------------------------------------------------------------ #

    def _check_quality(
        self, avg_entry: Decimal, stop: Decimal, atr: Decimal, report: AnalystReport
    ) -> RejectionReason | None:
        """§2 rules 3-5 against one specific weighted entry."""
        risk = self._config.risk
        stop_check = check_stop_distance(
            avg_entry=avg_entry,
            stop=stop,
            atr=atr,
            min_multiple=risk.stop_atr_min_multiple,
            max_multiple=risk.stop_atr_max_multiple,
        )
        if stop_check is not None:
            return stop_check
        rr = rr_multiples(avg_entry=avg_entry, stop=stop, targets=report.targets)
        return check_rr(rr=rr, min_rr_tp1=risk.min_rr_tp1)

    def _build_plan(
        self,
        *,
        report: AnalystReport,
        checked: _Inputs,
        sized: SizedLadder,
        stop: Decimal,
        stop_fraction: Decimal,
        leverage: int,
        margin_eur: Decimal,
        planned_risk_eur: Decimal,
        risk_eur: Decimal,
        rr_gross: tuple[Decimal, ...],
        rr_net: tuple[Decimal, ...],
        costs: PlanCosts,
        expiry: datetime,
        account: AccountState,
        market: MarketContext,
        now: datetime,
    ) -> TradePlan:
        return TradePlan(
            created_at=now,
            symbol=report.symbol,
            direction=report.direction,
            setup_type=report.setup_type,
            timeframe_label=report.timeframe_label,
            confidence=report.confidence,
            report=report,
            entries=sized.rungs,
            avg_entry=sized.avg_entry,
            avg_fill_price=sized.avg_fill_price,
            stop=stop,
            targets=report.targets,
            rr_targets=rr_gross,
            rr_targets_net=rr_net,
            # Measured on the FINAL ladder: a min-notional collapse moves
            # ``avg_entry``, and the plan that ships must show the distances of
            # the ladder it ships (M4 decision 2, applied to the display figures).
            target_distances_pct=tuple(
                distance_pct(sized.avg_entry, target, signed=False) for target in report.targets
            ),
            costs=costs,
            stop_distance_pct=percent(stop_fraction * HUNDRED),
            last_price=market.last_price,
            planned_risk_eur=planned_risk_eur,
            risk_eur=risk_eur,
            notional_usdt=sized.notional_usdt,
            notional_eur=sized.notional_eur,
            margin_eur=margin_eur,
            suggested_leverage=leverage,
            liq_distance_pct=percent(HUNDRED / leverage),
            liq_buffer_ok=True,
            management_plan=management_plan_text(self._config.management),
            expires_at=expiry,
            capital_eur=checked.capital_eur,
            risk_per_trade_pct=account.risk_per_trade_pct,
            eurusd_rate=account.eurusd_rate,
            instrument=checked.instrument,
        )

    def _decide(
        self,
        report: AnalystReport,
        now: datetime,
        status: GateStatus,
        reason: RejectionReason,
        *,
        detail: str = "",
    ) -> GateDecision:
        message = MESSAGES[reason]
        if detail:
            message = f"{message} ({detail})"
        log.info(
            "risk.rejected" if status is GateStatus.REJECTED else "risk.downgraded",
            symbol=report.symbol,
            reason=reason.value,
        )
        return GateDecision(
            symbol=report.symbol,
            status=status,
            reason=reason,
            message=message,
            evaluated_at=now,
            prompt_version=report.prompt_version,
        )


def _check_preconditions(
    report: AnalystReport, market: MarketContext, account: AccountState
) -> _Inputs | RejectionReason:
    """Resolve every optional input, or name the reason we cannot proceed."""
    if report.candidate_status is not CandidateStatus.CANDIDATE:
        return RejectionReason.NOT_A_CANDIDATE
    if (
        report.entry_zone is None
        or report.stop is None
        or not report.targets
        or report.direction is Direction.NONE
    ):
        return RejectionReason.MISSING_PLAN_FIELDS
    if account.capital_eur is None or account.capital_eur <= 0:
        return RejectionReason.NO_CAPITAL
    if account.eurusd_rate <= 0:
        return RejectionReason.FX_UNAVAILABLE
    if market.instrument is None:
        return RejectionReason.INSTRUMENT_META_MISSING
    if market.atr_1h is None or market.atr_1h <= 0:
        return RejectionReason.ATR_UNAVAILABLE
    return _Inputs(
        zone=report.entry_zone,
        stop=report.stop,
        atr=market.atr_1h,
        instrument=market.instrument,
        capital_eur=account.capital_eur,
    )
