"""Fixtures for the risk-engine suite (specs/RISK_ENGINE.md §8).

Instrument rules are the **real** values recorded from Binance USDT-M in M1
(``tests/cassettes/binance_market_*.json``), because §4's min-notional rule turns
on them: BTCUSDT's exchange minimum is 50 USDT, SOLUSDT's is 5 — so the engine's
``max(exchange_min, 20)`` gives 50 and 20 respectively, and hardcoding either
number would be wrong for the other symbol.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, settings

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
from sentinel.risk.models import AccountState, MarketContext, PauseState, PortfolioState

#: Deterministic "now" for every expiry/cooldown/pause assertion.
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

# Property tests must be reproducible in CI: same examples every run, no deadline
# flakes on a loaded machine.
settings.register_profile(
    "risk",
    max_examples=300,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("risk")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def config() -> AppConfig:
    """Spec defaults straight from the repo's ``config.yaml``."""
    return load_config()


def instrument(
    symbol: str,
    *,
    tick_size: str,
    qty_step: str,
    min_notional: str,
) -> InstrumentMeta:
    return InstrumentMeta(
        source="binance_usdm",
        fetched_at=NOW,
        symbol=symbol,
        tick_size=Decimal(tick_size),
        qty_step=Decimal(qty_step),
        min_notional=Decimal(min_notional),
    )


#: Recorded live in M1 — see tests/cassettes/binance_market_SOLUSDT.json.
SOLUSDT = instrument("SOLUSDT", tick_size="0.01", qty_step="0.01", min_notional="5")
#: BTCUSDT's exchange minimum (50) is above the 20 USDT practicality floor.
BTCUSDT = instrument("BTCUSDT", tick_size="0.1", qty_step="0.001", min_notional="50")


@pytest.fixture
def sol() -> InstrumentMeta:
    return SOLUSDT


@pytest.fixture
def btc() -> InstrumentMeta:
    return BTCUSDT


def report(
    *,
    symbol: str = "SOLUSDT",
    direction: Direction = Direction.LONG,
    zone: tuple[str, str] = ("82.10", "83.10"),
    stop: str = "81.20",
    #: TP1 was 84.90 through M4/M5 — gross RR 1.51, which clears §2 rule 5 on the
    #: old gross reading and fails it on the §4.2 net one (1.41R after €3.16 of
    #: fees). Moved to 85.20 (1.71 gross / 1.60 net) so the baseline stays an
    #: *approved* plan; the 84.90 case now has its own rejection golden.
    targets: tuple[str, ...] = ("85.20", "86.60", "88.90"),
    confidence: int = 78,
    status: CandidateStatus = CandidateStatus.CANDIDATE,
    timeframe_label: TimeframeLabel = TimeframeLabel.INTRADAY,
    setup_type: SetupType = SetupType.TREND_PULLBACK,
) -> AnalystReport:
    """A coherent baseline report; every test perturbs exactly one field."""
    return AnalystReport(
        symbol=symbol,
        candidate_status=status,
        setup_type=setup_type,
        direction=direction,
        timeframe_label=timeframe_label,
        thesis="4h uptrend intact; 1h pullback into EMA50 and the prior breakout level.",
        evidence=(Evidence(claim="4h trend is up", source_field="features.4h.trend_regime"),),
        counter_thesis="F&G at 74 (greed) — crowded-long risk.",
        entry_zone=EntryZone(low=Decimal(zone[0]), high=Decimal(zone[1])),
        stop=Decimal(stop),
        targets=tuple(Decimal(t) for t in targets),
        invalidation_price=Decimal("81.40"),
        invalidation_text="1h close below 81.40",
        confidence=confidence,
        prompt_version="v1",
    )


@pytest.fixture
def sol_long() -> AnalystReport:
    return report()


def market(
    *,
    symbol: str = "SOLUSDT",
    last_price: str = "83.40",
    atr_1h: str = "0.90",
    meta: InstrumentMeta | None = None,
    funding_rate: str | None = None,
    next_funding_time: datetime | None = None,
) -> MarketContext:
    """The baseline carries **no** funding rate, so the golden cases isolate fees.

    Funding has its own tests, where the rate is stated explicitly — mixing an
    estimated cost into hand-calculated fee goldens would blur both.
    """
    return MarketContext(
        symbol=symbol,
        last_price=Decimal(last_price),
        atr_1h=Decimal(atr_1h),
        instrument=meta or SOLUSDT,
        funding_rate=None if funding_rate is None else Decimal(funding_rate),
        next_funding_time=next_funding_time,
    )


@pytest.fixture
def sol_market() -> MarketContext:
    return market()


def account(
    *,
    capital_eur: str | None = "10000",
    risk_per_trade_pct: str = "0.75",
    eurusd_rate: str = "1.1593",
) -> AccountState:
    return AccountState(
        capital_eur=None if capital_eur is None else Decimal(capital_eur),
        risk_per_trade_pct=Decimal(risk_per_trade_pct),
        eurusd_rate=Decimal(eurusd_rate),
    )


@pytest.fixture
def acct() -> AccountState:
    return account()


def portfolio(
    *,
    open_risk_pct: str = "0",
    open_positions: int = 0,
    cooldowns: dict[str, datetime] | None = None,
    pause: PauseState | None = None,
) -> PortfolioState:
    return PortfolioState(
        open_risk_pct=Decimal(open_risk_pct),
        open_positions=open_positions,
        cooldown_until=cooldowns or {},
        pause=pause or PauseState(),
    )


@pytest.fixture
def empty_portfolio() -> PortfolioState:
    return portfolio()
