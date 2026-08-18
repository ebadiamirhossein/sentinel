"""Shared fixtures. No test in this suite touches a live database or a live API."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.core.config import AppConfig, Secrets, Settings, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG = REPO_ROOT / "config.yaml"


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
