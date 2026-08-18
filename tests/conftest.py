"""Shared fixtures. No test in this suite touches a live database or a live API."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from sentinel.charts.models import ChartImage
from sentinel.core.config import AppConfig, FeaturesConfig, Secrets, Settings, load_config
from sentinel.features import compute as compute_features
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import MarketSnapshot
from sentinel.llm.client import AnthropicClient
from tests.anthropic_double import ClientFactory, Recorder, make_client, scripted_transport
from tests.market_double import chart_album, snapshot_from_cassettes

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG = REPO_ROOT / "config.yaml"
CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guarantee that the suite never reaches a live API (CLAUDE.md).

    Any code path that tries to open a real socket fails loudly instead of
    quietly depending on the network. Tests marked ``@pytest.mark.allow_socket``
    opt out — that marker exists only for the opt-in local-Postgres round-trip
    tests, never for an external API.
    """
    if request.node.get_closest_marker("allow_socket") is not None:
        return

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("network access is not allowed in tests — replay a cassette instead")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def cassette(name: str) -> Any:
    """Load a recorded payload from ``tests/cassettes``."""
    path = CASSETTE_DIR / name
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


#: The instant the cassettes were recorded — anchors staleness assertions.
CASSETTE_NOW = datetime(2026, 8, 18, 8, 15, tzinfo=UTC)


@pytest.fixture
def repo_config() -> AppConfig:
    """The real ``config.yaml`` shipped with the repo."""
    return load_config(REPO_CONFIG)


@pytest.fixture
def secrets() -> Secrets:
    """Secrets with defaults only — never reads a developer's local ``.env``."""
    return Secrets(_env_file=None)


@pytest.fixture
def settings(secrets: Secrets, repo_config: AppConfig) -> Settings:
    return Settings(secrets=secrets, config=repo_config)


class StubDatabase:
    """Stands in for :class:`sentinel.storage.db.Database` in tests."""

    def __init__(self, *, healthy: bool = True, raises: Exception | None = None) -> None:
        self.healthy = healthy
        self.raises = raises
        self.disposed = False

    async def ping(self) -> bool:
        if self.raises is not None:
            raise self.raises
        return self.healthy

    async def dispose(self) -> None:
        self.disposed = True


# --------------------------------------------------------------------------- #
# LLM doubles (M5). Defined here rather than in tests/llm/ because three test
# packages -- llm, screener, analyst -- all drive the same wire-level double.
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_config() -> AppConfig:
    return load_config(REPO_CONFIG)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def client_factory(app_config: AppConfig, recorder: Recorder) -> ClientFactory:
    """Build a client that replays ``responses`` and records what was sent."""

    def build(
        responses: list[dict[str, Any] | httpx.Response],
        *,
        max_retries: int | None = None,
    ) -> AnthropicClient:
        return make_client(
            scripted_transport(responses, recorder), app_config, max_retries=max_retries
        )

    return build


# --------------------------------------------------------------------------- #
# Market-data fixtures. Cassette-backed; shared by the screener and analyst
# packages (tests/charts has its own, scoped to its rendering needs).
# --------------------------------------------------------------------------- #


@pytest.fixture
def snapshot() -> MarketSnapshot:
    return snapshot_from_cassettes()


@pytest.fixture
def features(snapshot: MarketSnapshot) -> SymbolFeatures:
    return compute_features(snapshot, FeaturesConfig())


@pytest.fixture
def snapshot_with_features(snapshot: MarketSnapshot, features: SymbolFeatures) -> MarketSnapshot:
    return snapshot.model_copy(update={"features": features.model_dump(mode="json")})


@pytest.fixture
def charts() -> list[ChartImage]:
    return chart_album()
