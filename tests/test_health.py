"""/health — the M0 demo criterion. No live database involved."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient

from sentinel import __version__
from sentinel.core.app import create_app
from sentinel.core.config import Settings
from tests.conftest import StubDatabase


def test_health_ok_when_db_and_scheduler_are_up(settings: Settings) -> None:
    database = StubDatabase(healthy=True)

    with TestClient(create_app(settings, database)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "scheduler": "running"}
    assert body["version"] == __version__
    # ``None`` means "no cycle has ever completed", which is true of a freshly
    # built app with no scheduler run behind it. Once the orchestrator has run,
    # this is a real age — tests/core/test_orchestrator.py asserts that, and the
    # value survives a restart because it is seeded from the ``cycles`` table.
    assert body["last_cycle_age_seconds"] is None
    assert body["uptime_seconds"] >= 0
    assert database.disposed is True  # lifespan shutdown ran


def test_health_degrades_to_503_when_db_is_down(settings: Settings) -> None:
    """DATA_SOURCES.md §4 / CLAUDE.md: degrade explicitly, never pretend to be fine."""
    with TestClient(create_app(settings, StubDatabase(healthy=False))) as client:
        response = client.get("/health")

    assert response.status_code == 503
    body: dict[str, Any] = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "error"


def test_health_degrades_when_the_db_probe_raises(settings: Settings) -> None:
    """An unreachable host raises socket.gaierror/OSError, not SQLAlchemyError.

    Regression: that path used to escape as a 500 ("app broken") instead of a
    503 ("dependency down") — caught by stopping postgres under Compose.
    """
    import socket

    database = StubDatabase(raises=socket.gaierror("Name or service not known"))

    with TestClient(create_app(settings, database)) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "error"


def test_database_ping_swallows_driver_errors() -> None:
    """`Database.ping` contract: returns False, never raises."""
    from sentinel.storage.db import Database

    async def probe() -> bool:
        db = Database("postgresql+asyncpg://sentinel:x@no-such-host.invalid:5432/sentinel")
        try:
            return await db.ping()
        finally:
            await db.dispose()

    assert asyncio.run(probe()) is False


def test_scheduler_heartbeat_runs(settings: Settings) -> None:
    with TestClient(create_app(settings, StubDatabase())) as client:
        body: dict[str, Any] = client.get("/health").json()

    assert body["checks"]["scheduler"] == "running"
    assert body["last_heartbeat_age_seconds"] is not None


def test_no_execution_surface_is_exposed(settings: Settings) -> None:
    """v1 has exactly one route. Any trade-shaped endpoint would be a hard violation."""
    app = create_app(settings, StubDatabase())
    paths = {route.path for route in app.routes if hasattr(route, "path")}

    assert paths == {"/health"}
