"""FastAPI application — ``/health`` only.

ARCHITECTURE.md §2: "plain async app + APScheduler for cycles; FastAPI only for
/health". The scheduler started here carries a heartbeat job in M0; the cycle
orchestrator is wired into it in M7.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from sentinel import __version__
from sentinel.bot.app import BotRunner, build_runner
from sentinel.core.clock import utc_now
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging, get_logger
from sentinel.storage.db import Database, SupportsPing

log = get_logger(__name__)


class HealthChecks(BaseModel):
    database: Literal["ok", "error"]
    scheduler: Literal["running", "stopped"]


class HealthResponse(BaseModel):
    """Scheduler heartbeat + DB ping + last-cycle age (ARCHITECTURE.md §5)."""

    status: Literal["ok", "degraded"]
    version: str
    environment: str
    now: datetime
    uptime_seconds: float
    last_cycle_age_seconds: float | None
    last_heartbeat_age_seconds: float | None
    checks: HealthChecks


@dataclass
class AppState:
    """Process-wide runtime state, reachable from handlers via ``request.app.state``."""

    settings: Settings
    database: SupportsPing
    scheduler: AsyncIOScheduler
    started_at: datetime = field(default_factory=utc_now)
    last_heartbeat_at: datetime | None = None
    #: Set by the cycle orchestrator once it exists (M7). None means "never ran".
    last_cycle_at: datetime | None = None
    #: The Telegram bot (M6). None when no token is configured — the app still
    #: serves /health and the scheduler still runs; only the UI is absent.
    bot: BotRunner | None = None


def _age_seconds(moment: datetime | None, *, now: datetime) -> float | None:
    if moment is None:
        return None
    return (now - moment).total_seconds()


def create_app(
    settings: Settings | None = None,
    database: SupportsPing | None = None,
) -> FastAPI:
    """Build the app. Both dependencies are injectable so tests need no live DB."""
    resolved = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(
            resolved.secrets.log_level,
            json_logs=resolved.secrets.json_logs,
        )
        db = database if database is not None else Database(resolved.secrets.database_url)
        scheduler = AsyncIOScheduler(timezone=UTC)

        state = AppState(settings=resolved, database=db, scheduler=scheduler)
        app.state.sentinel = state

        def heartbeat() -> None:
            state.last_heartbeat_at = utc_now()
            log.debug("scheduler.heartbeat", at=state.last_heartbeat_at.isoformat())

        scheduler.add_job(
            heartbeat,
            trigger="interval",
            seconds=resolved.config.schedule.heartbeat_interval_seconds,
            id="heartbeat",
            replace_existing=True,
            next_run_time=utc_now(),
        )
        scheduler.start()

        # specs/TELEGRAM_UX.md §1's UI, in this process and this event loop. A
        # missing token is a degraded start, not a crash: /health, the scheduler
        # and (from M7) the tracker are all still useful without a chat attached,
        # and refusing to boot would turn a misconfigured .env into an outage.
        if resolved.secrets.telegram_bot_token is None:
            log.warning("bot.disabled", reason="TELEGRAM_BOT_TOKEN is not set")
        elif isinstance(db, Database):
            state.bot = build_runner(resolved, db)
            await state.bot.start()
        else:  # pragma: no cover — only an injected test double lands here
            log.warning("bot.disabled", reason="no real database in this process")

        log.info(
            "app.started",
            version=__version__,
            environment=resolved.secrets.sentinel_env,
            watchlist_size=len(resolved.config.watchlist),
            scan_interval_minutes=resolved.config.schedule.scan_interval_minutes,
        )

        try:
            yield
        finally:
            if state.bot is not None:
                await state.bot.stop()
            scheduler.shutdown(wait=False)
            await db.dispose()
            log.info("app.stopped")

    app = FastAPI(
        title="Sentinel",
        version=__version__,
        description="Crypto market-research and signal system. Analysis only — never trades.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request, response: Response) -> HealthResponse:
        state: AppState = request.app.state.sentinel
        now = utc_now()

        # /health must always answer. A raising probe would become a 500, which
        # reads as "app broken" rather than "dependency down".
        try:
            db_ok = await state.database.ping()
        except Exception as exc:
            log.warning("health.db_probe_failed", error=str(exc), error_type=type(exc).__name__)
            db_ok = False

        scheduler_running = bool(state.scheduler.running)
        healthy = db_ok and scheduler_running

        if not healthy:
            response.status_code = 503

        return HealthResponse(
            status="ok" if healthy else "degraded",
            version=__version__,
            environment=state.settings.secrets.sentinel_env,
            now=now,
            uptime_seconds=(now - state.started_at).total_seconds(),
            last_cycle_age_seconds=_age_seconds(state.last_cycle_at, now=now),
            last_heartbeat_age_seconds=_age_seconds(state.last_heartbeat_at, now=now),
            checks=HealthChecks(
                database="ok" if db_ok else "error",
                scheduler="running" if scheduler_running else "stopped",
            ),
        )

    return app
