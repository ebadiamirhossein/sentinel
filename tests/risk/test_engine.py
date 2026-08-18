"""End-to-end gate behaviour: a complete §6 TradePlan, and an auditable decision."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.analyst.models import TimeframeLabel
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.features.models import SymbolFeatures, TimeframeFeatures
from sentinel.ingestion.models import Candle, MarketSnapshot, OHLCVSeries
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import GateStatus, MarketContext, RejectionReason

from .conftest import NOW, SOLUSDT, account, market, portfolio, report


def test_an_approved_plan_carries_every_field_the_card_needs(
    config: AppConfig, clock: FrozenClock
) -> None:
    """specs/RISK_ENGINE.md §6 — nothing on the signal card is computed by the bot."""
    decision = RiskEngine(config, clock=clock).evaluate(
        report=report(), market=market(), account=account(), portfolio=portfolio()
    )
    plan = decision.plan
    assert plan is not None

    assert plan.symbol == "SOLUSDT"
    assert plan.entries and all(
        e.price > 0 and e.qty > 0 and e.notional_eur > 0 and e.weight_pct > 0 for e in plan.entries
    )
    assert plan.avg_entry > 0
    assert plan.stop == Decimal("81.20")
    assert plan.targets == (Decimal("84.90"), Decimal("86.60"), Decimal("88.90"))
    assert len(plan.rr_targets) == len(plan.targets)
    assert plan.risk_eur > 0
    assert plan.margin_eur > 0
    assert plan.notional_eur > 0
    assert plan.suggested_leverage >= 1
    assert plan.liq_buffer_ok is True
    assert plan.management_plan
    assert plan.expires_at == NOW.replace(hour=0, day=19)
    assert plan.gate_status is GateStatus.APPROVED_FOR_HUMAN
    # Inputs are captured with the plan: sizing is immutable once issued (§7).
    assert plan.capital_eur == Decimal("10000")
    assert plan.risk_per_trade_pct == Decimal("0.75")
    assert plan.eurusd_rate == Decimal("1.1593")
    assert plan.instrument.min_notional == Decimal("5")
    # The analyst's own words travel with the plan (ARCHITECTURE.md contract 4).
    assert plan.report.thesis
    assert plan.report.prompt_version == "v1"


def test_the_plan_never_contains_leverage_or_size_from_the_analyst(
    config: AppConfig, clock: FrozenClock
) -> None:
    """The LLM contract has no sizing fields at all — prove it structurally."""
    fields = set(report().model_dump())
    assert not fields & {"qty", "leverage", "margin_eur", "notional_eur", "risk_eur"}


@pytest.mark.parametrize(
    ("label", "expected_hours"),
    [(TimeframeLabel.INTRADAY, 12), (TimeframeLabel.SWING, 36)],
)
def test_expiry_follows_the_timeframe_label(
    config: AppConfig, clock: FrozenClock, label: TimeframeLabel, expected_hours: int
) -> None:
    decision = RiskEngine(config, clock=clock).evaluate(
        report=report(timeframe_label=label),
        market=market(),
        account=account(),
        portfolio=portfolio(),
    )
    assert decision.plan is not None
    assert (decision.plan.expires_at - NOW).total_seconds() == expected_hours * 3600


def test_every_decision_is_json_serializable_for_the_audit_trail(
    config: AppConfig, clock: FrozenClock
) -> None:
    """PRD F10 / M9 stats: the reason code must survive the round trip to Postgres."""
    engine = RiskEngine(config, clock=clock)
    for rep in (report(), report(confidence=10), report(stop="82.50")):
        decision = engine.evaluate(
            report=rep, market=market(), account=account(), portfolio=portfolio()
        )
        payload = json.loads(json.dumps(decision.model_dump(mode="json")))
        assert payload["status"] == decision.status.value
        assert payload["reason"] == (None if decision.reason is None else decision.reason.value)
        assert payload["symbol"] == "SOLUSDT"


def test_rejected_and_downgraded_decisions_name_a_code_not_just_prose(
    config: AppConfig, clock: FrozenClock
) -> None:
    engine = RiskEngine(config, clock=clock)
    downgraded = engine.evaluate(
        report=report(confidence=10), market=market(), account=account(), portfolio=portfolio()
    )
    assert downgraded.status is GateStatus.DOWNGRADED_WATCHLIST
    assert downgraded.reason is RejectionReason.LOW_CONFIDENCE
    assert downgraded.message != downgraded.reason.value  # prose *and* a code
    assert "10" in downgraded.message  # the offending confidence, for the audit trail


def test_market_context_is_built_from_a_snapshot_and_its_features(config: AppConfig) -> None:
    """M7 wires the pipeline; the engine must not reach into ingestion itself."""
    candle = Candle(
        open_time=datetime(2026, 8, 18, 11, 0, tzinfo=UTC),
        open=Decimal("82"),
        high=Decimal("84"),
        low=Decimal("81"),
        close=Decimal("83"),
        volume=Decimal("100"),
    )
    snapshot = MarketSnapshot(
        symbol="SOLUSDT",
        captured_at=NOW,
        last_price=Decimal("83.40"),
        ohlcv={
            "1h": OHLCVSeries(
                source="binance_usdm",
                fetched_at=NOW,
                symbol="SOLUSDT",
                timeframe="1h",
                candles=(candle,),
            )
        },
        instrument=SOLUSDT,
    )
    features = SymbolFeatures(
        symbol="SOLUSDT",
        computed_at=NOW,
        reference_price=Decimal("83.40"),
        timeframes={
            "1h": TimeframeFeatures(
                timeframe="1h",
                candles_used=200,
                last_close=Decimal("83.20"),
                last_closed_at=NOW,
                partial_candle_dropped=True,
                atr14=Decimal("0.90"),
            )
        },
    )

    context = MarketContext.from_snapshot(snapshot, features)
    assert context.symbol == "SOLUSDT"
    assert context.last_price == Decimal("83.40")
    assert context.atr_1h == Decimal("0.90")
    assert context.instrument is not None
    assert context.instrument.min_notional == Decimal("5")


def test_missing_features_degrade_to_no_atr_rather_than_a_guess(config: AppConfig) -> None:
    snapshot = MarketSnapshot(
        symbol="SOLUSDT",
        captured_at=NOW,
        last_price=Decimal("83.40"),
        ohlcv={},
        instrument=SOLUSDT,
    )
    context = MarketContext.from_snapshot(snapshot, None)
    assert context.atr_1h is None
