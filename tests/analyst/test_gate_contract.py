"""M5's output feeds M4's input. This is the seam, tested end to end without a call.

The risk engine has been able to size a plan since M4; until now nothing produced
the ``AnalystReport`` it consumes. If this test passes, the pipeline is connected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.analyst.models import AnalystReportPayload
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.ingestion.models import InstrumentMeta
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import AccountState, GateStatus, MarketContext, PortfolioState
from tests.market_double import valid_report_json

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def sol_instrument() -> InstrumentMeta:
    return InstrumentMeta(
        source="binance_usdm",
        fetched_at=NOW,
        symbol="SOLUSDT",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.01"),
        min_notional=Decimal("5"),
    )


def sol_payload(**overrides: object) -> AnalystReportPayload:
    """The M4 worked example (journal/M4_REPORT.md §3), as the model would emit it."""
    base = {
        "symbol": "SOLUSDT",
        "entry_zone": {"low": 82.10, "high": 83.10},
        "stop": 81.20,
        "targets": [84.90, 86.60, 88.90],
        "invalidation_price": 81.20,
        "confidence": 78,
    }
    base.update(overrides)
    return AnalystReportPayload.model_validate_json(valid_report_json(**base))  # type: ignore[arg-type]


@pytest.fixture
def engine(app_config: AppConfig) -> RiskEngine:
    return RiskEngine(app_config, clock=FrozenClock(NOW))


def market() -> MarketContext:
    return MarketContext(
        symbol="SOLUSDT",
        last_price=Decimal("83.40"),
        atr_1h=Decimal("0.90"),
        instrument=sol_instrument(),
    )


def account() -> AccountState:
    return AccountState(capital_eur=Decimal("10000"), eurusd_rate=Decimal("1.1593"))


def test_a_validated_payload_produces_an_approved_plan(engine: RiskEngine) -> None:
    report = sol_payload().to_report(prompt_version="fable_v1", model="claude-fable-5")

    decision = engine.evaluate(
        report=report, market=market(), account=account(), portfolio=PortfolioState()
    )

    assert decision.status is GateStatus.APPROVED_FOR_HUMAN
    assert decision.plan is not None
    assert decision.plan.symbol == "SOLUSDT"
    assert decision.plan.margin_eur > 0


def test_prompt_version_travels_into_the_gate_decision(engine: RiskEngine) -> None:
    """specs/PROMPTS.md §5's A/B key has to survive the whole pipeline."""
    report = sol_payload().to_report(prompt_version="fable_v1", model="claude-fable-5")

    decision = engine.evaluate(
        report=report, market=market(), account=account(), portfolio=PortfolioState()
    )

    assert decision.prompt_version == "fable_v1"


def test_the_analyst_cannot_smuggle_sizing_into_the_plan() -> None:
    """CLAUDE.md: the LLM never sizes. The wire schema has no field for it."""
    assert not {"capital_eur", "leverage", "margin_eur", "qty"} & set(
        AnalystReportPayload.model_fields
    )


def test_a_low_confidence_candidate_is_downgraded_not_approved(engine: RiskEngine) -> None:
    """The gate, not the prompt, enforces the confidence floor (§2 rule 6)."""
    report = sol_payload(confidence=40).to_report(prompt_version="fable_v1", model="claude-fable-5")

    decision = engine.evaluate(
        report=report, market=market(), account=account(), portfolio=PortfolioState()
    )

    assert decision.status is GateStatus.DOWNGRADED_WATCHLIST


def test_an_incoherent_report_is_rejected_with_a_code(engine: RiskEngine) -> None:
    """Schema-valid is not the same as coherent; the gate is the second check."""
    report = sol_payload(stop=90.0).to_report(prompt_version="fable_v1", model="claude-fable-5")

    decision = engine.evaluate(
        report=report, market=market(), account=account(), portfolio=PortfolioState()
    )

    assert decision.status is GateStatus.REJECTED
    assert decision.reason is not None
