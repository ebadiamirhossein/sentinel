"""structlog output shape and — most importantly — secret redaction."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from sentinel.core.logging import configure_logging, get_logger


@pytest.fixture
def log_stream() -> io.StringIO:
    stream = io.StringIO()
    configure_logging("DEBUG", json_logs=True, stream=stream)
    return stream


def _last_record(stream: io.StringIO) -> dict[str, Any]:
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert lines, "nothing was logged"
    parsed: dict[str, Any] = json.loads(lines[-1])
    return parsed


def test_json_record_shape(log_stream: io.StringIO) -> None:
    get_logger("test").info("cycle.completed", symbols=12)
    record = _last_record(log_stream)

    assert record["event"] == "cycle.completed"
    assert record["level"] == "info"
    assert record["symbols"] == 12
    assert record["logger"] == "test"


def test_timestamps_are_utc(log_stream: io.StringIO) -> None:
    """CLAUDE.md: all timestamps UTC internally."""
    get_logger("test").info("tick")
    stamp = _last_record(log_stream)["timestamp"]

    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(None)


@pytest.mark.parametrize(
    "key",
    [
        "anthropic_api_key",
        "telegram_bot_token",
        "api_key",
        "password",
        "authorization",
        "client_secret",
    ],
)
def test_credential_shaped_keys_are_redacted(log_stream: io.StringIO, key: str) -> None:
    get_logger("test").info("llm.call", **{key: "super-secret-value"})
    record = _last_record(log_stream)

    assert record[key] == "***redacted***"
    assert "super-secret-value" not in json.dumps(record)


def test_secretstr_values_are_redacted(log_stream: io.StringIO) -> None:
    get_logger("test").info("boot", credential=SecretStr("super-secret-value"))
    record = _last_record(log_stream)

    assert record["credential"] == "***redacted***"
    assert "super-secret-value" not in json.dumps(record)


def test_llm_call_accounting_fields_survive(log_stream: io.StringIO) -> None:
    """CLAUDE.md requires logging tokens in/out — `tokens_*` must not trip redaction."""
    get_logger("test").info(
        "llm.call",
        model="claude-fable-5",
        prompt_version="v1",
        tokens_in=1200,
        tokens_out=430,
        cost_usd=0.04,
        duration_ms=8100,
    )
    record = _last_record(log_stream)

    assert record["model"] == "claude-fable-5"
    assert record["tokens_in"] == 1200
    assert record["tokens_out"] == 430
    assert record["prompt_version"] == "v1"
    assert record["cost_usd"] == 0.04


def test_stdlib_logs_are_rendered_too(log_stream: io.StringIO) -> None:
    """uvicorn and library logs must land in the same JSON stream."""
    logging.getLogger("uvicorn.error").warning("started on port %s", 8000)
    record = _last_record(log_stream)

    assert record["event"] == "started on port 8000"
    assert record["level"] == "warning"


def test_console_mode_is_plain_text() -> None:
    stream = io.StringIO()
    configure_logging("INFO", json_logs=False, stream=stream)
    get_logger("test").info("dev.mode", symbol="BTCUSDT")

    output = stream.getvalue()
    assert "dev.mode" in output
    assert "BTCUSDT" in output
    with pytest.raises(json.JSONDecodeError):
        json.loads(output.splitlines()[-1])
