"""Builders for risk-engine inputs, shared beyond ``tests/risk/``.

M6's card tests need a real, approved ``TradePlan`` — a hand-built one would let a
renderer bug hide behind a fixture that never went through the gate. The builders
themselves live in ``tests/risk/conftest.py``, and importing one package's
conftest from another is the cross-wiring ``market_double.py`` warns about, so the
shared shapes live here and both packages import from one place.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from sentinel.ingestion.models import InstrumentMeta
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import (
    AccountState,
    GateDecision,
    GateStatus,
    MarketContext,
    PortfolioState,
    TradePlan,
)

#: The instant every M6 fixture is frozen at, so a rendered card is reproducible.
PLAN_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: Recorded live from Binance USDT-M in M1 (tests/cassettes/binance_market_SOLUSDT.json).
SOLUSDT = InstrumentMeta(
    source="binance_usdm",
    fetched_at=PLAN_NOW,
    symbol="SOLUSDT",
    tick_size=Decimal("0.01"),
    qty_step=Decimal("0.01"),
    min_notional=Decimal("5"),
)


def analyst_report(
    *,
    symbol: str = "SOLUSDT",
    direction: Direction = Direction.LONG,
    zone: tuple[str, str] = ("82.10", "83.10"),
    stop: str = "81.20",
    targets: tuple[str, ...] = ("85.20", "86.60", "88.90"),
    confidence: int = 78,
    status: CandidateStatus = CandidateStatus.CANDIDATE,
) -> AnalystReport:
    """The same baseline the risk goldens use — approved, and coherent by hand."""
    return AnalystReport(
        symbol=symbol,
        candidate_status=status,
        setup_type=SetupType.TREND_PULLBACK,
        direction=direction,
        timeframe_label=TimeframeLabel.INTRADAY,
        thesis=(
            "4h uptrend intact; 1h pullback into EMA50 and the prior breakout level 82.4 "
            "with volume drying up on the retrace. Funding neutral, OI rising with price."
        ),
        evidence=(Evidence(claim="4h trend is up", source_field="features.4h.trend_regime"),),
        counter_thesis="Fear & Greed at 74 (greed) — crowded-long risk into resistance at 84.6.",
        entry_zone=EntryZone(low=Decimal(zone[0]), high=Decimal(zone[1])),
        stop=Decimal(stop),
        targets=tuple(Decimal(t) for t in targets),
        invalidation_price=Decimal("81.40"),
        invalidation_text="1h close below 81.40",
        confidence=confidence,
        prompt_version="fable_v1",
        model="claude-fable-5",
    )


def market_context(
    *,
    last_price: str = "83.40",
    funding_rate: str | None = "0.0000193",
    next_funding_time: datetime | None = datetime(2026, 8, 18, 16, 0, tzinfo=UTC),
) -> MarketContext:
    """Carries the funding rate on purpose: a card must show the cost block."""
    return MarketContext(
        symbol="SOLUSDT",
        last_price=Decimal(last_price),
        atr_1h=Decimal("0.90"),
        instrument=SOLUSDT,
        funding_rate=None if funding_rate is None else Decimal(funding_rate),
        next_funding_time=next_funding_time,
    )


def account(*, capital_eur: str | None = "10000") -> AccountState:
    return AccountState(
        capital_eur=None if capital_eur is None else Decimal(capital_eur),
        risk_per_trade_pct=Decimal("0.75"),
        eurusd_rate=Decimal("1.1593"),
    )


def decide(config: AppConfig | None = None, **kwargs: object) -> GateDecision:
    """Run the real gate. Nothing in the bot suite fabricates a plan."""
    report = kwargs.pop("report", None) or analyst_report()
    market = kwargs.pop("market", None) or market_context()
    state = kwargs.pop("account", None) or account()
    assert isinstance(report, AnalystReport)
    assert isinstance(market, MarketContext)
    assert isinstance(state, AccountState)
    return RiskEngine(config or load_config(), clock=FrozenClock(PLAN_NOW)).evaluate(
        report=report,
        market=market,
        account=state,
        portfolio=PortfolioState(),
    )


def approved_plan(config: AppConfig | None = None, **kwargs: object) -> TradePlan:
    decision = decide(config, **kwargs)
    assert decision.status is GateStatus.APPROVED_FOR_HUMAN, decision.message
    assert decision.plan is not None
    return decision.plan


__all__ = [
    "PLAN_NOW",
    "SOLUSDT",
    "account",
    "analyst_report",
    "approved_plan",
    "decide",
    "market_context",
]
