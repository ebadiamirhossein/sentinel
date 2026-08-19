"""Gate-decision audit rows and pause state (§7: "pause state persisted, survives restart").

Serialization is tested always; the Postgres round-trip is opt-in, exactly like
the M1 ingestion tests:

    SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://sentinel:change-me@localhost:5432/sentinel \\
        .venv/bin/pytest tests/risk/test_persistence.py
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import (
    GateDecision,
    GateStatus,
    PauseReason,
    PauseState,
    RejectionReason,
)
from sentinel.storage.models import GateDecisionRow, RiskStateRow
from sentinel.storage.repositories import GateDecisionRepository, RiskStateRepository
from tests.bot_double import OWNER_ID
from tests.db_guard import TEST_DB_URL, requires_db

from .conftest import NOW, account, market, portfolio, report


def decisions(config: AppConfig, clock: FrozenClock) -> list[GateDecision]:
    engine = RiskEngine(config, clock=clock)
    return [
        engine.evaluate(report=rep, market=market(), account=account(), portfolio=portfolio())
        for rep in (report(), report(confidence=10), report(stop="82.50"))
    ]


# ── pure serialization (no database) ─────────────────────────────────────────


def test_decision_rows_keep_the_machine_readable_reason(
    config: AppConfig, clock: FrozenClock
) -> None:
    rows = [GateDecisionRepository.to_row(d, user_id=OWNER_ID) for d in decisions(config, clock)]

    assert [row["gate_status"] for row in rows] == [
        GateStatus.APPROVED_FOR_HUMAN.value,
        GateStatus.DOWNGRADED_WATCHLIST.value,
        GateStatus.REJECTED.value,
    ]
    assert [row["reason"] for row in rows] == [
        None,
        RejectionReason.LOW_CONFIDENCE.value,
        RejectionReason.STOP_SIDE.value,
    ]
    assert rows[0]["plan"]["suggested_leverage"] == 5
    assert rows[1]["plan"] is None
    assert all(row["symbol"] == "SOLUSDT" for row in rows)


def test_decision_rows_are_json_safe(config: AppConfig, clock: FrozenClock) -> None:
    import json

    plan = GateDecisionRepository.to_row(decisions(config, clock)[0], user_id=OWNER_ID)["plan"]
    assert plan is not None
    assert json.loads(json.dumps(plan))["entries"][0]["qty"] == "18.30"


# ── Postgres round-trip (opt-in) ─────────────────────────────────────────────


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert TEST_DB_URL is not None
    engine = create_async_engine(TEST_DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()
        for table in (GateDecisionRow, RiskStateRow):
            await session.execute(delete(table))
        await session.commit()
    await engine.dispose()


@requires_db
async def test_gate_decisions_round_trip(
    session: AsyncSession, config: AppConfig, clock: FrozenClock
) -> None:
    repo = GateDecisionRepository(session)
    for decision in decisions(config, clock):
        await repo.record(decision, user_id=OWNER_ID)
    await session.commit()

    stored = await repo.recent(limit=10)
    assert {row.reason for row in stored} == {
        None,
        RejectionReason.LOW_CONFIDENCE.value,
        RejectionReason.STOP_SIDE.value,
    }
    approved = next(row for row in stored if row.gate_status == "APPROVED_FOR_HUMAN")
    assert approved.plan is not None
    assert approved.plan["risk_eur"] == "74.98"


@requires_db
async def test_pause_state_survives_a_restart(session: AsyncSession) -> None:
    """§7 — a loss-limit pause must still be in force after the process dies."""
    repo = RiskStateRepository(session)
    assert (await repo.load()) == PauseState()

    paused = PauseState(
        paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=NOW + timedelta(hours=24)
    )
    await repo.save(paused)
    await session.commit()

    # A fresh repository over a fresh session is the restart.
    reloaded = await RiskStateRepository(session).load()
    assert reloaded == paused
    assert reloaded.is_active(NOW + timedelta(hours=23)) is True
    assert reloaded.is_active(NOW + timedelta(hours=25)) is False


@requires_db
async def test_resuming_clears_the_pause(session: AsyncSession) -> None:
    repo = RiskStateRepository(session)
    await repo.save(PauseState(paused=True, reason=PauseReason.MANUAL))
    await session.commit()

    await repo.save(PauseState())
    await session.commit()

    assert (await repo.load()).paused is False
    assert (await repo.load()).reason is None
