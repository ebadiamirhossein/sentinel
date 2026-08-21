"""Builders for forex-gate inputs, shared beyond ``tests/fx/``.

The same discipline ``tests/risk_double.py`` sets and for the same reason: **nothing in
this suite fabricates a plan.** A hand-built ``ForexPlan`` would let a card bug, a
tracker bug or a dispatch bug hide behind a fixture that never went through the gate —
and every R figure downstream is measured against ``planned_risk_eur``, which only the
gate sets.

The numbers are chosen to sit comfortably inside every rail rather than on any edge, so
a test that fails here has failed for its own reason. The edges get their own tests in
``test_gate.py``, one on each side, deliberately.

**EURUSD at €10,000, not the owner's €200.** journal/M10b_REPORT.md §6 measured that at
€200 the widest EURUSD stop that sizes at all is 17.5 pips, so a shared fixture at that
capital would spend its life asserting ``BELOW_MIN_TICKET``. €200 is the subject of its
own tests, where the rejection is the point.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sentinel.analyst.models import (
    AnalystReport,
    CandidateStatus,
    Direction,
    EntryZone,
    Evidence,
    SetupType,
    TimeframeLabel,
)
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, load_config
from sentinel.fx.calendar import CalendarEvent, EconomicCalendar, Impact
from sentinel.fx.gate import ForexAccountState, ForexGate, ForexMarketContext
from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.models import ForexGateStatus
from sentinel.fx.plan import ForexGateDecision, ForexPlan
from sentinel.fx.rails import ForexPortfolioState
from sentinel.fx.spread import SpreadProfile

#: A Wednesday, mid-London, clear of the 19:00-22:00 rollover band and of the Friday
#: cutoff — so no clock rail is incidentally load-bearing in an unrelated test.
FX_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)

#: Verified against the live API on 2026-08-21 — Uic 21, Format.Decimals 4, pip 1e-4
#: cross-checked against TickSize x 10, MinimumTradeSize 1000 (journal/M10b_REPORT.md §8).
EURUSD = ForexInstrument(
    symbol="EURUSD",
    uic=21,
    decimals=4,
    pip=Decimal("0.0001"),
    tick_size=Decimal("0.00001"),
    min_trade_size=Decimal("1000"),
    amount_decimals=2,
    base_currency="EUR",
    quote_currency="USD",
    resolved_at=FX_NOW,
)

#: The yen cross, where the pip is 0.01 and the rate is EURJPY. Present in the shared
#: builders because §7.1's ~145x error is the one a fixture set of majors would miss.
USDJPY = ForexInstrument(
    symbol="USDJPY",
    uic=42,
    decimals=2,
    pip=Decimal("0.01"),
    tick_size=Decimal("0.001"),
    min_trade_size=Decimal("1000"),
    amount_decimals=2,
    base_currency="USD",
    quote_currency="JPY",
    resolved_at=FX_NOW,
)


def spread_profile(
    *, symbol: str = "EURUSD", median: str = "1.1", samples: int = 1180
) -> SpreadProfile:
    """A profile shaped like the spike's measurements — EURUSD's median is 1.1 pips.

    Every hour carries the same median except 21:00, which carries the rollover figure
    the spike actually measured. That asymmetry is the point: a flat profile would make
    the per-hour cost baseline indistinguishable from the global gate baseline, and
    defect #15 is precisely about those being different numbers.
    """
    base = Decimal(median)
    by_hour = dict.fromkeys(range(24), base)
    by_hour[21] = Decimal("2.7")
    return SpreadProfile(
        symbol=symbol,
        samples=samples,
        global_median_pips=base,
        median_by_hour_pips=by_hour,
        min_pips=Decimal("1.0"),
        max_pips=Decimal("18.3"),
    )


def calendar(*, events: tuple[CalendarEvent, ...] = (), covered: bool = True) -> EconomicCalendar:
    """A calendar with coverage and, by default, nothing scheduled.

    The **shipped** calendar claims no coverage at all, so every gate call against it
    rejects with ``CALENDAR_STALE`` — correct, and it would make every other test in the
    suite vacuous. ``covered=False`` gets that behaviour back when it is the subject.
    """
    return EconomicCalendar(
        events=events,
        coverage_until=date(2026, 12, 31) if covered else None,
        source="tests/fx/forex_double.py",
    )


def high_impact(*, at: datetime, currency: str = "USD", name: str = "CPI") -> CalendarEvent:
    return CalendarEvent(at=at, currency=currency, impact=Impact.HIGH, name=name)


def analyst_report(
    *,
    symbol: str = "EURUSD",
    direction: Direction = Direction.LONG,
    zone: tuple[str, str] = ("1.16700", "1.16800"),
    stop: str = "1.16400",
    targets: tuple[str, ...] = ("1.17600", "1.18100", "1.18800"),
    confidence: int = 78,
    status: CandidateStatus = CandidateStatus.CANDIDATE,
    timeframe_label: TimeframeLabel = TimeframeLabel.INTRADAY,
) -> AnalystReport:
    """A coherent EURUSD long: a 35-pip stop and TP1 at roughly 2.4R gross.

    Comfortably clear of the 1.5 net gate at a 1.1-pip spread, and comfortably inside
    the 0.6-3.0 ATR stop band at the 15-pip ATR below. Both of those are checked on
    their own edges elsewhere.
    """
    return AnalystReport(
        symbol=symbol,
        candidate_status=status,
        setup_type=SetupType.TREND_PULLBACK,
        direction=direction,
        timeframe_label=timeframe_label,
        thesis=(
            "4h uptrend intact; 1h pullback into the prior-day open at 1.1670 during the "
            "London-New York overlap. Spread is at its normal median for this hour."
        ),
        evidence=(
            Evidence(claim="prior-day open holds", source_field="forex.prior_day.open"),
            Evidence(claim="London-NY overlap", source_field="forex.session_now"),
        ),
        counter_thesis=(
            "All three pairs cross the dollar and the USD strength index is rising — this "
            "may be a dollar view rather than a EURUSD one."
        ),
        entry_zone=EntryZone(low=Decimal(zone[0]), high=Decimal(zone[1])),
        stop=Decimal(stop),
        targets=tuple(Decimal(t) for t in targets),
        invalidation_price=Decimal("1.16350"),
        invalidation_text="1h close below 1.1635",
        confidence=confidence,
        prompt_version="fable_forex_v1",
        model="claude-fable-5",
    )


def market_context(
    *,
    last_price: str = "1.16850",
    atr_1h: str | None = "0.00150",
    instrument: ForexInstrument | None = EURUSD,
    current_spread_pips: str | None = "1.1",
    profile: SpreadProfile | None = None,
    candles_stale: bool = False,
) -> ForexMarketContext:
    return ForexMarketContext(
        symbol="EURUSD" if instrument is None else instrument.symbol,
        last_price=Decimal(last_price),
        atr_1h=None if atr_1h is None else Decimal(atr_1h),
        instrument=instrument,
        spread_profile=spread_profile() if profile is None else profile,
        current_spread_pips=(None if current_spread_pips is None else Decimal(current_spread_pips)),
        candles_stale=candles_stale,
    )


def account(*, capital_eur: str | None = "10000", rate: str | None = "1.169") -> ForexAccountState:
    return ForexAccountState(
        capital_eur=None if capital_eur is None else Decimal(capital_eur),
        risk_per_trade_pct=Decimal("0.75"),
        eur_quote_rate=None if rate is None else Decimal(rate),
    )


def decide(
    config: AppConfig | None = None,
    *,
    report: AnalystReport | None = None,
    market: ForexMarketContext | None = None,
    state: ForexAccountState | None = None,
    portfolio: ForexPortfolioState | None = None,
    events: EconomicCalendar | None = None,
    now: datetime = FX_NOW,
) -> ForexGateDecision:
    """Run the real gate. Nothing in this suite fabricates a plan."""
    gate = ForexGate(
        config or load_config(),
        calendar=events or calendar(),
        clock=FrozenClock(now),
    )
    return gate.evaluate(
        report=report or analyst_report(),
        market=market or market_context(),
        account=state or account(),
        portfolio=portfolio or ForexPortfolioState(),
    )


def forex_plan(config: AppConfig | None = None, **kwargs: object) -> ForexPlan:
    decision = decide(config, **kwargs)  # type: ignore[arg-type]
    assert decision.status is ForexGateStatus.APPROVED_FOR_HUMAN, decision.message
    assert decision.plan is not None
    return decision.plan


__all__ = [
    "EURUSD",
    "FX_NOW",
    "USDJPY",
    "account",
    "analyst_report",
    "calendar",
    "decide",
    "forex_plan",
    "high_impact",
    "market_context",
    "spread_profile",
]
