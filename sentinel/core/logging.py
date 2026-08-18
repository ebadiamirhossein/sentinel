"""structlog setup: JSON in prod, human-readable in dev, UTC timestamps, redaction.

CLAUDE.md forbids logging secrets, so redaction is a processor in the chain rather
than a convention: any event key that looks like a credential is replaced before a
renderer ever sees it, and ``SecretStr`` values are rendered as ``***redacted***``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog
from pydantic import SecretStr
from structlog.typing import EventDict, Processor, WrappedLogger

_REDACTED = "***redacted***"

# Matched on the whole key or a trailing segment — never on a bare substring, so
# legitimate fields like `tokens_in` / `tokens_out` (which CLAUDE.md requires us to
# log for every LLM call) are not swallowed by the `token` rule.
_SENSITIVE_EXACT = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "password",
        "passwd",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "api_key",
        "apikey",
        "key",
    }
)
_SENSITIVE_SUFFIX = (
    "_token",
    "_secret",
    "_password",
    "_passwd",
    "_key",
    "_apikey",
    "_credential",
    "_credentials",
    "_authorization",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_EXACT or lowered.endswith(_SENSITIVE_SUFFIX)


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Scrub credential-shaped keys and any ``SecretStr`` value."""
    for key, value in event_dict.items():
        if isinstance(value, SecretStr) or (
            isinstance(key, str) and value is not None and _is_sensitive_key(key)
        ):
            event_dict[key] = _REDACTED
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(
    level: str = "INFO",
    *,
    json_logs: bool = True,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog **and** the stdlib root logger (so uvicorn logs match).

    Safe to call more than once; the last call wins.
    """
    shared = _shared_processors()
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=stream is None and sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger."""
    return structlog.stdlib.get_logger(name)
