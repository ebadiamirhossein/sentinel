"""The module tree from ARCHITECTURE.md §3 exists and imports cleanly."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from sentinel.core.clock import FrozenClock, SystemClock, utc_now

MODULES = [
    "sentinel.ingestion",
    "sentinel.ingestion.adapters",
    "sentinel.features",
    "sentinel.charts",
    "sentinel.screener",
    "sentinel.analyst",
    "sentinel.risk",
    "sentinel.bot",
    "sentinel.tracker",
    "sentinel.stats",
    "sentinel.storage",
    "sentinel.core",
    "sentinel.tools",
    "sentinel.main",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module: str) -> None:
    assert importlib.import_module(module) is not None


def test_utc_now_is_timezone_aware() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)
    assert SystemClock().now().tzinfo is not None


def test_frozen_clock_requires_awareness() -> None:
    frozen_at = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    assert FrozenClock(frozen_at).now() == frozen_at

    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 8, 18, 12, 0))  # noqa: DTZ001 — that is the point
