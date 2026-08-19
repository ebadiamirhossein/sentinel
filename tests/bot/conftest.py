"""Fixtures for the M6 suite. Nothing here opens a socket or a real Bot."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.models import SignalRecord
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, load_config
from sentinel.risk.models import TradePlan
from tests.bot_double import OWNER_ID, FakeBot, FakeDatabase
from tests.risk_double import PLAN_NOW, approved_plan

#: The owner's timezone from the repo's config.yaml, so tests read the same card
#: the owner would (Europe/Vilnius is UTC+3 in August — a real offset, not zero,
#: which is the point: a UTC-only bug would pass silently against UTC).
OWNER_TZ = ZoneInfo("Europe/Vilnius")


@pytest.fixture
def bot_config() -> AppConfig:
    return load_config()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(PLAN_NOW)


@pytest.fixture
def tz() -> ZoneInfo:
    return OWNER_TZ


@pytest.fixture
def plan(bot_config: AppConfig) -> TradePlan:
    """A genuinely gate-approved plan — never hand-assembled."""
    return approved_plan(bot_config)


@pytest.fixture
def record(plan: TradePlan) -> SignalRecord:
    return SignalRecord(plan=plan, user_id=OWNER_ID, number=1)


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def fake_database() -> FakeDatabase:
    return FakeDatabase()
