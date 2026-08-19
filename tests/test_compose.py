"""The deployment promises that are only made in a YAML file (M8).

The target server already runs an unrelated application and its own Postgres, so
docker-compose.yml carries three constraints that are invisible to every other
test in this suite: no published database port, a fixed project name, and a
health port bound to loopback. A promise about somebody else's production box
that lives only in a runbook is a promise that regresses the first time someone
"just adds a port for debugging" — journal/M6_REPORT.md §12 is the same lesson
about packaging, and it cost two milestones.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def dev_compose() -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_project_name_is_pinned(compose: dict[str, Any]) -> None:
    """Without ``name:``, Compose names the project after the directory — so a
    clone into ``sentinel/`` and a clone into ``ai-trader/`` are different stacks
    with different volumes, and either could collide with the neighbour app."""
    assert compose["name"] == "sentinel"


def test_postgres_publishes_no_host_port(compose: dict[str, Any]) -> None:
    """5432 on the target host belongs to another application.

    The app reaches the database over the Compose network, where no publish is
    needed at all. docker-compose.dev.yml adds one for local tooling, and is
    never used on the server.
    """
    assert "ports" not in compose["services"]["postgres"]


def test_the_health_port_is_bound_to_loopback(compose: dict[str, Any]) -> None:
    """Sentinel needs no inbound port: Telegram is long polling, i.e. outbound.

    Anything published on 0.0.0.0 here would be a service exposed to the internet
    for no reason, on a box the owner shares with something else.
    """
    (published,) = compose["services"]["app"]["ports"]
    assert published.startswith("127.0.0.1:")
    assert "${SENTINEL_HTTP_PORT:-18080}" in published


def test_the_development_override_is_loopback_too(dev_compose: dict[str, Any]) -> None:
    (published,) = dev_compose["services"]["postgres"]["ports"]
    assert published.startswith("127.0.0.1:")


@pytest.mark.parametrize("service", ["app", "postgres"])
def test_every_service_restarts_itself(compose: dict[str, Any], service: str) -> None:
    """This is what brings the stack back after a reboot — docs/DEPLOY.md §10."""
    assert compose["services"][service]["restart"] == "unless-stopped"


@pytest.mark.parametrize("service", ["app", "postgres"])
def test_every_service_caps_its_logs(compose: dict[str, Any], service: str) -> None:
    """Docker's json-file driver is unlimited by default and this app logs JSON
    every minute, for ever. The cap is per service rather than in the daemon
    config, which the neighbour app also depends on."""
    options = compose["services"][service]["logging"]["options"]
    assert options["max-size"] == "10m"
    assert int(options["max-file"]) >= 2
