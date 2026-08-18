"""Entrypoint: ``python -m sentinel.main``."""

from __future__ import annotations

import uvicorn

from sentinel.core.app import create_app
from sentinel.core.config import load_settings
from sentinel.core.logging import configure_logging, get_logger


def main() -> None:
    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)
    log = get_logger(__name__)
    log.info(
        "main.boot",
        host=settings.secrets.health_host,
        port=settings.secrets.health_port,
        environment=settings.secrets.sentinel_env,
    )

    uvicorn.run(
        create_app(settings),
        host=settings.secrets.health_host,
        port=settings.secrets.health_port,
        log_config=None,  # keep our structlog handlers
        access_log=False,
    )


if __name__ == "__main__":
    main()
