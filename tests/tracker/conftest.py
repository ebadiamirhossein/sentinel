"""Fixtures for the tracker suite.

Plans come from ``tests/risk_double.approved_plan``, which runs the **real** risk
engine — the same reason M6 gave for building card fixtures that way. A
hand-built ``TradePlan`` would let a tracker bug hide behind a ladder that never
passed the gate, and every R figure below is measured against ``planned_risk_eur``,
which only the engine sets.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.bot.models import SignalStatus
from sentinel.core.config import AppConfig, Secrets, Settings, load_config
from sentinel.ingestion.models import Candle
from sentinel.risk.models import TradePlan
from sentinel.tracker.models import LegExit, RungFill, SignalTracking
from tests.risk_double import PLAN_NOW, analyst_report, approved_plan, market_context

#: The instant every plan in this suite was built at, from ``risk_double``.
NOW = PLAN_NOW
LATER = NOW + timedelta(hours=1)


@pytest.fixture
def config() -> AppConfig:
    return load_config()


@pytest.fixture
def settings(config: AppConfig) -> Settings:
    return Settings(secrets=Secrets(_env_file=None), config=config)


@pytest.fixture
def plan(config: AppConfig) -> TradePlan:
    """The M4 golden ladder: 18.30 @ 83.10, 21.73 @ 82.60, 24.15 @ 82.10."""
    return approved_plan(config)


@pytest.fixture
def short_plan(config: AppConfig) -> TradePlan:
    """The mirror of the baseline, so no test can pass by assuming a long."""
    from sentinel.analyst.models import Direction

    return approved_plan(
        config,
        report=analyst_report(
            direction=Direction.SHORT,
            zone=("83.70", "84.70"),
            stop=("85.60"),
            targets=("81.60", "80.20", "77.90"),
        ),
        market=market_context(last_price="83.40"),
    )


def tracking(
    plan: TradePlan,
    *,
    status: SignalStatus = SignalStatus.PENDING_ENTRY,
    fills: tuple[RungFill, ...] = (),
    exits: tuple[LegExit, ...] = (),
    stop_price: Decimal | None = None,
) -> SignalTracking:
    return SignalTracking(plan=plan, status=status, fills=fills, exits=exits, stop_price=stop_price)


def fill(plan: TradePlan, index: int, *, at: datetime = LATER) -> RungFill:
    """A fill of one rung at exactly its limit price — what detection produces."""
    rung = plan.entries[index]
    return RungFill(rung_index=index, price=rung.price, qty=rung.qty, at=at)


def candle(
    *,
    at: datetime = LATER,
    open: str,
    high: str,
    low: str,
    close: str,
) -> Candle:
    return Candle(
        open_time=at,
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
    )


__all__ = ["LATER", "NOW", "candle", "fill", "tracking"]
