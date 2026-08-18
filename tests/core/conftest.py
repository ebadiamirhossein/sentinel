"""Fixtures for the cycle orchestrator suite.

The orchestrator is the one module that touches everything, so its tests drive it
against fakes for the two things that cost money — the Anthropic API and Telegram
— and a real risk engine, real cards and a real feature engine underneath. A test
that faked the gate would prove the orchestrator calls something, not that it
ships the plan the engine approved.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.models import SignalRecord
from sentinel.core.config import Secrets, Settings, load_config
from sentinel.risk.models import PauseState

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def tz() -> ZoneInfo:
    return ZoneInfo("Europe/Vilnius")


@pytest.fixture
def plan() -> Any:
    """A real gate-approved plan — never a hand-built one (M6's rule)."""
    from tests.risk_double import approved_plan

    return approved_plan(load_config())


@pytest.fixture
def settings() -> Settings:
    secrets = Secrets(_env_file=None, ANTHROPIC_API_KEY="test-key")
    return Settings(secrets=secrets, config=load_config())


@dataclass
class CycleStore:
    """The slice of Postgres a cycle reads and writes."""

    settings: dict[str, Any] = field(default_factory=dict)
    pause: PauseState = field(default_factory=PauseState)
    signals: dict[UUID, Any] = field(default_factory=dict)
    cycles: dict[UUID, Any] = field(default_factory=dict)
    snapshots: list[Any] = field(default_factory=list)
    reports: list[Any] = field(default_factory=list)
    gate_decisions: list[Any] = field(default_factory=list)
    llm_calls: list[Any] = field(default_factory=list)
    spend_day: Decimal = Decimal("0")
    open_symbols: set[str] = field(default_factory=set)
    resolutions: list[tuple[str, datetime]] = field(default_factory=list)
    published_today: int = 0
    committed: int = 0


class CycleSession:
    def __init__(self, store: CycleStore) -> None:
        self.store = store

    async def commit(self) -> None:
        self.store.committed += 1

    async def rollback(self) -> None:  # pragma: no cover
        pass


class CycleDatabase:
    def __init__(self, store: CycleStore | None = None) -> None:
        self.store = store or CycleStore()

    def session(self) -> Any:
        @asynccontextmanager
        async def _session() -> Any:
            yield CycleSession(self.store)

        return _session()


def signal_row(record: SignalRecord) -> Any:
    """A ``SignalRow``-shaped object for the store, without a database."""
    plan = record.plan
    return type(
        "Row",
        (),
        {
            "id": record.signal_id,
            "plan_id": plan.plan_id,
            "number": record.number or 1,
            "symbol": plan.symbol,
            "plan": plan.model_dump(mode="json"),
            "status": record.status.value,
            "decision": None,
            "dry_run": record.dry_run,
            "created_at": plan.created_at,
            "expires_at": plan.expires_at,
        },
    )()


__all__ = ["NOW", "CycleDatabase", "CycleSession", "CycleStore", "signal_row"]
