"""The forex gate (FOREX.md §16.5) — a composition, not new arithmetic.

Every rail this runs already existed as a pure verdict function: the clock in
:mod:`sentinel.fx.hours`, the calendar in :mod:`sentinel.fx.calendar`, the spread in
:mod:`sentinel.fx.spread`, the sizing in :mod:`sentinel.fx.ladder` and the cost model in
:mod:`sentinel.fx.costs`. What this module adds is the **order**, the preconditions and
the assembly of a :class:`~sentinel.fx.plan.ForexPlan`.

The order is the one :mod:`sentinel.risk.engine` established, and two properties of it
are load-bearing rather than tidy:

* **the cheap, most-specific rails come first.** A shut market, a currency-matched
  blackout and a rollover spread each make everything after them moot, and each is
  established without touching the analyst's numbers. A rejection then names the thing
  that actually stopped the setup rather than a downstream symptom of it;
* **net RR is last.** After the clock, the calendar, the spread, the geometry, the
  rails, the ladder and the margin — so the harder blocker always wins. A setup that
  was paused *and* had a poor RR reports the pause, because that is the answer the
  person who typed ``/pause`` is looking for.

Two baselines for one spread, and they are different numbers on purpose (spec defect
#15): the **gate** compares the current spread to the instrument's **global** median, so
rollover hours fail naturally; the **cost model** prices it at this **hour-of-day's**
median, which is the right expectation of what trading in this hour costs. Using the
per-hour median for both would normalise away exactly the widening the gate exists to
catch, and the gate would never fire at the one hour it was written for.

**One import from the frozen package, and it is deliberate.**
``sentinel.risk.management.management_plan_text`` builds the "TP1: close 40%, move stop
to breakeven" line from the ``management:`` config block. It is pure, it takes config and
returns a string, and both markets are managed by the *same* block — so duplicating it
would create two managements that drift apart while one config claims to set both. That
is a worse failure than the coupling it avoids, and it is the opposite trade-off from
``fx/rounding.py``, where the duplicated thing is arithmetic that genuinely differs.
Nothing else here imports ``sentinel/risk/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sentinel.analyst.models import AnalystReport, CandidateStatus, EntryZone
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import AppConfig
from sentinel.core.logging import get_logger
from sentinel.fx.calendar import EconomicCalendar
from sentinel.fx.coherence import (
    check_confidence,
    check_entry_distance,
    check_geometry,
    check_rr,
    check_stop_distance,
    rr_multiples,
)
from sentinel.fx.costs import ForexCosts, estimate_costs, net_rr, ratio, rollover_nights
from sentinel.fx.expiry import carries_weekend_gap_risk, plan_expiry
from sentinel.fx.hours import clock_verdict
from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.ladder import SizedLadder, build_ladder, size_ladder
from sentinel.fx.models import ForexGateStatus, ForexRejection
from sentinel.fx.plan import ForexGateDecision, ForexPlan
from sentinel.fx.rails import ForexPortfolioState, check_portfolio_rails
from sentinel.fx.rounding import money, percent, pip_value
from sentinel.fx.sizing import ForexSizing, pips_between, risk_budget_eur
from sentinel.fx.spread import SpreadProfile, quantize_pips, spread_gate
from sentinel.risk.management import management_plan_text

log = get_logger(__name__)

HUNDRED = Decimal("100")


#: One line per code, shown to the owner. A meta-test asserts the mapping is total:
#: a rejection with no wording is a card that says nothing, which is the
#: silence-as-success failure this project has met three times.
MESSAGES: dict[ForexRejection, str] = {
    ForexRejection.MARKET_CLOSED: "the forex market is closed",
    ForexRejection.WEEK_OPEN_QUIET: "the trading week has only just opened",
    ForexRejection.FRIDAY_CUTOFF: "past the Friday cutoff — a ladder placed now cannot fill",
    ForexRejection.ROLLOVER_WINDOW: "inside the rollover window, where spreads widen",
    ForexRejection.SPREAD_TOO_WIDE: "the spread is far above this instrument's normal median",
    ForexRejection.EVENT_BLACKOUT: "a high-impact event for this pair is imminent",
    ForexRejection.CALENDAR_STALE: "the economic calendar cannot be trusted right now",
    ForexRejection.BELOW_MIN_TICKET: "the intended risk sizes below the venue's minimum ticket",
    ForexRejection.NET_RR_TOO_LOW: "reward-to-risk is too low once the spread is charged",
    ForexRejection.NO_CAPITAL: "no capital is set, so nothing can be sized",
    ForexRejection.FX_RATE_UNAVAILABLE: "no EUR conversion rate, so nothing can be sized",
    ForexRejection.STALE_CANDLES: "the newest candle is too old while the market is open",
    ForexRejection.MAX_CONCURRENT_POSITIONS: "a forex position is already open",
    ForexRejection.NOT_A_CANDIDATE: "the analyst did not put this forward as a candidate",
    ForexRejection.MISSING_PLAN_FIELDS: "the candidate is missing an entry, a stop or a target",
    ForexRejection.INSTRUMENT_UNRESOLVED: "the instrument could not be resolved at this venue",
    ForexRejection.ATR_UNAVAILABLE: "no ATR, so the stop distance cannot be checked",
    ForexRejection.ENTRY_ZONE_INVALID: "the entry zone is not a valid zone",
    ForexRejection.STOP_SIDE: "the stop is on the wrong side of the entry",
    ForexRejection.TARGET_ORDER: "the targets are not ordered away from the entry",
    ForexRejection.ENTRY_TOO_FAR: "the entry sits too far from the current price",
    ForexRejection.STOP_TOO_TIGHT: "the stop is inside the noise",
    ForexRejection.STOP_TOO_WIDE: "the stop is too wide for this instrument's volatility",
    ForexRejection.RR_TOO_LOW: "reward-to-risk is too low before costs are even charged",
    ForexRejection.LOW_CONFIDENCE: "confidence is below the bar for a sized plan",
    ForexRejection.PAUSED: "forex signals are paused",
    ForexRejection.SYMBOL_COOLDOWN: "this pair is still inside its cooldown",
    ForexRejection.DAILY_SIGNAL_CAP: "the daily forex signal cap has been reached",
    ForexRejection.MARGIN_ABOVE_EQUITY_SHARE: (
        "required margin is above the permitted share of equity"
    ),
}


@dataclass(frozen=True)
class ForexMarketContext:
    """What the gate needs to know about the instrument, right now.

    Not an import of :class:`sentinel.risk.models.MarketContext`: that carries a funding
    rate and an ``InstrumentMeta``, neither of which exists here, and it lives in the
    frozen package.
    """

    symbol: str
    last_price: Decimal
    atr_1h: Decimal | None = None
    instrument: ForexInstrument | None = None
    #: The gate's baseline lives on this; the cost model's per-hour one does too.
    spread_profile: SpreadProfile | None = None
    #: The most recent measured spread, in pips.
    current_spread_pips: Decimal | None = None
    #: The **observed** week open when the tail reached a weekend; ``None`` falls back
    #: to the nominal one, which is the honest fallback rather than a guess.
    week_open: datetime | None = None
    #: Set when the cycle already found this symbol's candles too old (§5.1, #14).
    candles_stale: bool = False


@dataclass(frozen=True)
class ForexAccountState:
    """One person's sizing inputs. ``eur_quote_rate``, never ``eurusd_rate`` (§7.1)."""

    capital_eur: Decimal | None = None
    risk_per_trade_pct: Decimal = Decimal("0.75")
    #: How many quote-currency units one euro buys — EURUSD for the majors, **EURJPY**
    #: for USDJPY. Using EURUSD there is a ~145x error that sizes a position to roughly
    #: nothing and looks like an unremarkable rejection.
    eur_quote_rate: Decimal | None = None


@dataclass(frozen=True)
class _Inputs:
    """Every ``Optional`` resolved once, so nothing below re-asks."""

    zone: EntryZone
    stop: Decimal
    atr: Decimal
    instrument: ForexInstrument
    capital_eur: Decimal
    eur_quote_rate: Decimal


class ForexGate:
    """CANDIDATE in, sized :class:`ForexPlan` or a named rejection out."""

    def __init__(
        self,
        config: AppConfig,
        *,
        calendar: EconomicCalendar,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._forex = config.forex
        self._calendar = calendar
        self._clock = clock or SystemClock()

    # ── the entry point ──────────────────────────────────────────────────────

    def evaluate(
        self,
        *,
        report: AnalystReport,
        market: ForexMarketContext,
        account: ForexAccountState,
        portfolio: ForexPortfolioState,
    ) -> ForexGateDecision:
        now = self._clock.now()

        # 1 — preconditions.
        checked = self._check_preconditions(report, market, account)
        if isinstance(checked, ForexRejection):
            status = (
                ForexGateStatus.DOWNGRADED_WATCHLIST
                if checked is ForexRejection.NOT_A_CANDIDATE
                else ForexGateStatus.REJECTED
            )
            return self._decide(report, market, now, checked, status=status)

        # 2, 3, 4 — the clock, the calendar and the spread, before any of the
        # analyst's numbers are touched. Each makes everything after it moot.
        gated = self._check_market_conditions(market, now)
        if gated is not None:
            return self._decide(report, market, now, gated)

        # 5a — geometry, entry distance, confidence.
        quality = check_geometry(
            direction=report.direction,
            zone=checked.zone,
            stop=checked.stop,
            targets=report.targets,
        ) or check_entry_distance(
            zone=checked.zone,
            last_price=market.last_price,
            max_distance_pct=self._forex.max_entry_distance_pct,
        )
        if quality is not None:
            return self._decide(report, market, now, quality)

        if check_confidence(
            confidence=report.confidence, min_confidence=self._forex.min_confidence
        ):
            return self._decide(
                report,
                market,
                now,
                ForexRejection.LOW_CONFIDENCE,
                status=ForexGateStatus.DOWNGRADED_WATCHLIST,
            )

        # 5b — draft the ladder and check the stop and gross RR against its weighted
        # entry, which is the basis every RR figure downstream also uses.
        rungs = build_ladder(
            zone=checked.zone,
            direction=report.direction,
            last_price=market.last_price,
            atr=checked.atr,
            single_entry_atr_threshold=self._config.ladder.single_entry_atr_threshold,
            weights_pct=self._config.ladder.weights_pct,
        )
        draft_entry = sum((rung.price * rung.weight_pct / HUNDRED for rung in rungs), Decimal(0))
        drafted = self._check_quality(draft_entry, checked, report)
        if drafted is not None:
            return self._decide(report, market, now, drafted)

        # 6 — rails.
        railed = check_portfolio_rails(
            portfolio=portfolio, symbol=market.symbol, config=self._forex, now=now
        )
        if railed is not None:
            detail = portfolio.pause_detail if railed is ForexRejection.PAUSED else ""
            return self._decide(report, market, now, railed, detail=detail)

        # 7 — size, collapsing while any rung is below the venue minimum.
        planned_risk_eur = risk_budget_eur(checked.capital_eur, account.risk_per_trade_pct)
        sized = size_ladder(
            rungs,
            zone=checked.zone,
            stop=checked.stop,
            instrument=checked.instrument,
            risk_eur=planned_risk_eur,
            eur_quote_rate=checked.eur_quote_rate,
            last_price=market.last_price,
        )
        if sized is None:
            return self._decide(
                report,
                market,
                now,
                ForexRejection.BELOW_MIN_TICKET,
                detail=(
                    f"even a single rung carrying the whole €{planned_risk_eur} budget is "
                    f"below the venue minimum of {checked.instrument.min_trade_size} units"
                ),
            )

        # 5c — re-check quality on the FINAL ladder: a collapse moves the weighted
        # entry, and the plan that ships must satisfy the rules it ships under.
        collapsed = self._check_quality(sized.avg_entry, checked, report)
        if collapsed is not None:
            return self._decide(
                report,
                market,
                now,
                collapsed,
                detail=f"after collapsing to {len(sized.rungs)} rung(s)",
            )

        # 8 — margin as a share of equity. Account-level (§7.6), never per position.
        margin_eur = money(sized.notional_eur / self._forex.max_leverage)
        margin_pct = (margin_eur / checked.capital_eur * HUNDRED).quantize(Decimal("0.01"))
        if margin_pct > self._forex.max_margin_pct_of_equity:
            return self._decide(
                report,
                market,
                now,
                ForexRejection.MARGIN_ABOVE_EQUITY_SHARE,
                detail=(
                    f"€{margin_eur} is {margin_pct}% of equity, above the "
                    f"{self._forex.max_margin_pct_of_equity}% ceiling"
                ),
            )

        # 9 — costs, then net RR. Last, so the harder blocker always wins.
        expires_at, expiry_basis = plan_expiry(
            created_at=now,
            timeframe_label=report.timeframe_label,
            management=self._config.management,
            forex=self._forex,
        )
        sizing = self._sizing_of(sized, checked, margin_eur, planned_risk_eur)
        costs = self._costs_of(sizing, market, now=now, expires_at=expires_at)
        gross = rr_multiples(avg_entry=sized.avg_entry, stop=checked.stop, targets=report.targets)
        net = tuple(net_rr(multiple, risk_eur=sized.risk_eur, costs=costs) for multiple in gross)
        if net[0] < self._forex.min_rr_tp1:
            return self._decide(
                report,
                market,
                now,
                ForexRejection.NET_RR_TOO_LOW,
                detail=(
                    f"{net[0]}R net against a {self._forex.min_rr_tp1}R gate "
                    f"({gross[0]}R gross, {costs.spread_pips} pip spread)"
                ),
            )

        plan = self._build_plan(
            report=report,
            market=market,
            account=account,
            checked=checked,
            sized=sized,
            sizing=sizing,
            costs=costs,
            gross=gross,
            net=net,
            margin_eur=margin_eur,
            margin_pct=margin_pct,
            planned_risk_eur=planned_risk_eur,
            expires_at=expires_at,
            expiry_basis=expiry_basis,
            now=now,
        )
        log.info(
            "forex.gate.approved",
            symbol=market.symbol,
            rungs=len(plan.entries),
            units=str(sized.units),
            stop_pips=str(plan.stop_distance_pips),
            spread_pips=str(costs.spread_pips),
            net_rr_tp1=str(net[0]),
        )
        return ForexGateDecision(
            symbol=market.symbol,
            status=ForexGateStatus.APPROVED_FOR_HUMAN,
            plan=plan,
            evaluated_at=now,
            prompt_version=report.prompt_version,
        )

    # ── the rows ─────────────────────────────────────────────────────────────

    def _check_preconditions(
        self,
        report: AnalystReport,
        market: ForexMarketContext,
        account: ForexAccountState,
    ) -> _Inputs | ForexRejection:
        if report.candidate_status is not CandidateStatus.CANDIDATE:
            return ForexRejection.NOT_A_CANDIDATE
        if report.entry_zone is None or report.stop is None or not report.targets:
            return ForexRejection.MISSING_PLAN_FIELDS
        if account.capital_eur is None or account.capital_eur <= 0:
            return ForexRejection.NO_CAPITAL
        if account.eur_quote_rate is None or account.eur_quote_rate <= 0:
            return ForexRejection.FX_RATE_UNAVAILABLE
        if market.instrument is None:
            return ForexRejection.INSTRUMENT_UNRESOLVED
        if market.atr_1h is None or market.atr_1h <= 0:
            return ForexRejection.ATR_UNAVAILABLE
        return _Inputs(
            zone=report.entry_zone,
            stop=report.stop,
            atr=market.atr_1h,
            instrument=market.instrument,
            capital_eur=account.capital_eur,
            eur_quote_rate=account.eur_quote_rate,
        )

    def _check_market_conditions(
        self, market: ForexMarketContext, now: datetime
    ) -> ForexRejection | None:
        """Rows 2, 3 and 4 — the clock, the calendar and the spread, in that order.

        Candle staleness is folded in here rather than into the preconditions because
        it is a fact about the market's data, and because §5.1 (as corrected by defect
        #14) makes it meaningful **only while the market is open** — which the clock
        check immediately above has just established.
        """
        clock = clock_verdict(now, config=self._forex, week_open=market.week_open)
        if clock.reason is not None:
            return clock.reason
        if market.candles_stale:
            return ForexRejection.STALE_CANDLES

        blackout = self._calendar.blackout(
            market.symbol,
            now,
            before_minutes=self._forex.blackout_before_minutes,
            after_minutes=self._forex.blackout_after_minutes,
            warn_within_days=self._forex.calendar_warn_within_days,
        )
        if blackout.rejection is not None:
            return blackout.rejection

        spread = spread_gate(
            current_pips=market.current_spread_pips,
            profile=market.spread_profile,
            now=now,
            config=self._forex,
        )
        if spread.rejection is not None:
            log.info(
                "forex.gate.spread_rejected",
                symbol=market.symbol,
                hour=now.hour,
                basis=spread.basis,
                current_pips=str(spread.current_pips),
                threshold_pips=str(spread.threshold_pips),
            )
        return spread.rejection

    def _check_quality(
        self, avg_entry: Decimal, checked: _Inputs, report: AnalystReport
    ) -> ForexRejection | None:
        stop = check_stop_distance(
            avg_entry=avg_entry,
            stop=checked.stop,
            atr=checked.atr,
            min_multiple=self._forex.stop_atr_min_multiple,
            max_multiple=self._forex.stop_atr_max_multiple,
        )
        if stop is not None:
            return stop
        gross = rr_multiples(avg_entry=avg_entry, stop=checked.stop, targets=report.targets)
        return check_rr(rr=gross, min_rr_tp1=self._forex.min_rr_tp1)

    # ── assembly ─────────────────────────────────────────────────────────────

    def _sizing_of(
        self,
        sized: SizedLadder,
        checked: _Inputs,
        margin_eur: Decimal,
        planned_risk_eur: Decimal,
    ) -> ForexSizing:
        """The ladder as a :class:`ForexSizing`, so ``estimate_costs`` needs no changes.

        Built from the **ladder's** aggregates rather than by re-running
        ``size_position`` on the weighted entry: those two would disagree whenever the
        ladder has more than one rung, and the one that priced the costs would not be
        the one the owner places.
        """
        instrument = checked.instrument
        return ForexSizing(
            symbol=instrument.symbol,
            quote_currency=instrument.quote_currency,
            units=sized.units,
            notional_quote=money(sized.notional_quote),
            notional_eur=sized.notional_eur,
            pip=instrument.pip,
            pip_value_eur=pip_value(sized.units * instrument.pip / checked.eur_quote_rate),
            stop_distance_pips=quantize_pips(
                pips_between(sized.avg_entry, checked.stop, instrument.pip)
            ),
            stop_distance_quote=abs(sized.avg_entry - checked.stop),
            entry_price=sized.avg_entry,
            stop_price=checked.stop,
            planned_risk_eur=planned_risk_eur,
            risk_eur=sized.risk_eur,
            margin_eur=margin_eur,
            max_leverage=self._forex.max_leverage,
            eur_quote_rate=checked.eur_quote_rate,
        )

    def _costs_of(
        self,
        sizing: ForexSizing,
        market: ForexMarketContext,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> ForexCosts:
        """Price the round trip at **this hour-of-day's** median spread (defect #15).

        The gate compared the *current* spread to the global median; the cost model
        prices the expected one for the hour the trade is actually in. Falls back to the
        current reading, and then to zero pips with the basis saying so — a spread we
        cannot measure must not silently read as free, and ``spread_basis`` is what
        carries that admission onto the card.
        """
        profile = market.spread_profile
        if profile is not None:
            spread_pips = profile.expected_at(now.hour)
            basis = f"median for {now.hour:02d}:00 UTC over {profile.samples} samples"
        elif market.current_spread_pips is not None:
            spread_pips = market.current_spread_pips
            basis = "the current reading — no spread profile was available"
        else:
            spread_pips = Decimal("0")
            basis = "UNMEASURED — no spread series was available for this cycle"

        swap = self._forex.swap_pips_per_night.get(sizing.symbol, {})
        return estimate_costs(
            sizing,
            spread_pips=spread_pips,
            spread_basis=basis,
            commission_per_million_quote=self._forex.commission_per_million_quote,
            rollover_pips_per_night=swap.get("long", Decimal("0")),
            nights=rollover_nights(
                now, expires_at, rollover_hour_utc=self._forex.rollover_hour_utc
            ),
            credit_favourable_rollover=self._forex.credit_favourable_rollover,
        )

    def _build_plan(
        self,
        *,
        report: AnalystReport,
        market: ForexMarketContext,
        account: ForexAccountState,
        checked: _Inputs,
        sized: SizedLadder,
        sizing: ForexSizing,
        costs: ForexCosts,
        gross: tuple[Decimal, ...],
        net: tuple[Decimal, ...],
        margin_eur: Decimal,
        margin_pct: Decimal,
        planned_risk_eur: Decimal,
        expires_at: datetime,
        expiry_basis: str,
        now: datetime,
    ) -> ForexPlan:
        instrument = checked.instrument
        stop_fraction = abs(sized.avg_entry - checked.stop) / sized.avg_entry
        return ForexPlan(
            created_at=now,
            symbol=market.symbol,
            direction=report.direction,
            setup_type=report.setup_type,
            timeframe_label=report.timeframe_label,
            confidence=report.confidence,
            report=report,
            entries=sized.rungs,
            avg_entry=sized.avg_entry,
            avg_fill_price=sized.avg_fill_price,
            stop=checked.stop,
            targets=report.targets,
            rr_targets=tuple(ratio(value) for value in gross),
            rr_targets_net=net,
            target_distances_pct=tuple(
                percent(abs(target - sized.avg_entry) / sized.avg_entry * HUNDRED)
                for target in report.targets
            ),
            target_distances_pips=tuple(
                quantize_pips(pips_between(target, sized.avg_entry, instrument.pip))
                for target in report.targets
            ),
            costs=costs,
            stop_distance_pct=percent(stop_fraction * HUNDRED),
            stop_distance_pips=sizing.stop_distance_pips,
            last_price=market.last_price,
            planned_risk_eur=planned_risk_eur,
            risk_eur=sized.risk_eur,
            notional_eur=sized.notional_eur,
            margin_eur=margin_eur,
            quote_currency=instrument.quote_currency,
            notional_quote=money(sized.notional_quote),
            pip=instrument.pip,
            pip_value_eur=sizing.pip_value_eur,
            eur_quote_rate=checked.eur_quote_rate,
            max_leverage=sizing.max_leverage,
            leverage_basis=sizing.leverage_basis,
            margin_pct_of_equity=margin_pct,
            weekend_gap_warning=carries_weekend_gap_risk(
                created_at=now,
                timeframe_label=report.timeframe_label,
                management=self._config.management,
                forex=self._forex,
            ),
            management_plan=management_plan_text(self._config.management),
            expires_at=expires_at,
            expiry_basis=expiry_basis,
            capital_eur=checked.capital_eur,
            risk_per_trade_pct=account.risk_per_trade_pct,
            instrument=instrument,
        )

    def _decide(
        self,
        report: AnalystReport,
        market: ForexMarketContext,
        now: datetime,
        reason: ForexRejection,
        *,
        status: ForexGateStatus = ForexGateStatus.REJECTED,
        detail: str = "",
    ) -> ForexGateDecision:
        message = MESSAGES[reason]
        if detail:
            message = f"{message} ({detail})"
        log.info(
            "forex.gate.rejected",
            symbol=market.symbol,
            reason=reason.value,
            status=status.value,
        )
        return ForexGateDecision(
            symbol=market.symbol,
            status=status,
            reason=reason,
            message=message,
            evaluated_at=now,
            prompt_version=report.prompt_version,
        )


__all__ = [
    "MESSAGES",
    "ForexAccountState",
    "ForexGate",
    "ForexMarketContext",
    "ForexPortfolioState",
]
