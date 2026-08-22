"""FastAPI application — ``/health`` only — and the scheduler that runs everything.

ARCHITECTURE.md §2: "plain async app + APScheduler for cycles; FastAPI only for
/health". M0 started the scheduler with a heartbeat and nothing else; M7 adds the
two jobs the system actually exists to run — the scan cycle and the 60-second
tracker tick.

Both are registered with ``max_instances=1`` and ``coalesce=True``. A scan that
overruns its interval (PRD F1 budgets five minutes against a fifteen-minute
period, but an LLM outage can stretch one) must never start a second copy against
the same database, and a backlog of missed ticks must run once rather than
fourteen times in a row.

``last_cycle_at`` is seeded from the ``cycles`` table at boot. An in-memory
timestamp would report "never ran" after every deploy — precisely the moment
``/health`` is being watched most closely.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from sentinel import __version__
from sentinel.bot.alerts import AdminAlerter
from sentinel.bot.app import BotRunner, build_runner
from sentinel.bot.cards import loss_pause_card
from sentinel.bot.formatting import zone_info
from sentinel.bot.models import ACK_VERSION
from sentinel.bot.notices import UserNotifier, loss_pause_key
from sentinel.bot.notifier import TrackerNotifier
from sentinel.bot.publisher import SignalPublisher
from sentinel.core.clock import utc_now
from sentinel.core.config import Settings, load_settings
from sentinel.core.forex_auth import ForexCredentialKeeper
from sentinel.core.logging import configure_logging, get_logger
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.core.orchestrator import CycleOrchestrator
from sentinel.core.wiring import forex_adapter, market_adapter
from sentinel.fx.pricing import ForexCandleSource
from sentinel.storage.db import Database, SupportsPing
from sentinel.storage.repositories import CycleRepository, UserRepository
from sentinel.tracker.loop import TrackerLoop
from sentinel.tracker.prices import DEFAULT_MAX_CANDLES, CandleSource, PriceFeed

log = get_logger(__name__)

#: The Saxo refresh job's id. Registered only when forex is enabled, so a crypto-only
#: deployment's job list is unchanged — which is what "deploying this is a no-op" rests
#: on and what `scheduler.pipeline_scheduled`'s `job_ids` now makes readable from the box.
FOREX_TOKEN_JOB = "forex-token-refresh"


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
    #: When the newest cycle finished. Seeded from ``cycles`` at boot, so it
    #: survives a restart; ``None`` means no cycle has ever completed.
    last_cycle_at: datetime | None = None
    #: The Telegram bot (M6). None when no token is configured — the app still
    #: serves /health and the scheduler still runs; only the UI is absent.
    bot: BotRunner | None = None


def _age_seconds(moment: datetime | None, *, now: datetime) -> float | None:
    if moment is None:
        return None
    return (now - moment).total_seconds()


async def _last_cycle_at(database: Database) -> datetime | None:
    """The newest completed cycle, so ``/health`` is honest immediately after a
    restart rather than for the first fifteen minutes claiming nothing ever ran."""
    try:
        async with database.session() as session:
            return await CycleRepository(session).latest_completed_at()
    except Exception as exc:  # pragma: no cover — a down DB is already reported
        log.warning("scheduler.last_cycle_unavailable", error=str(exc))
        return None


def _schedule_pipeline(
    scheduler: AsyncIOScheduler, state: AppState, settings: Settings, database: Database
) -> None:
    """The jobs ARCHITECTURE §3 describes — **one scan per enabled market** (M10a).

    Each swallows its own exceptions. An APScheduler job that raises is logged and
    then simply not run again on some configurations, and a tracker that stops
    because one tick failed would abandon live positions — the exact opposite of
    what it is for.

    A job per market rather than one job that loops over them, so each market keeps
    its own ``scan_interval_minutes`` and its own ``max_instances=1``: a forex scan
    that overran must not delay a crypto scan, and a crypto scan that crashed must
    not skip forex. With forex disabled exactly one job is registered, on the same
    60-minute trigger as before this milestone.
    """
    schedule = settings.config.schedule

    def scan_for(market: Market) -> Callable[[], Coroutine[Any, Any, None]]:
        async def scan() -> None:
            factory = (
                None if state.bot is None else _publisher_factory(state, settings, database, market)
            )
            notices = None if state.bot is None else _notices_for(state, settings, database)
            try:
                result = await CycleOrchestrator(
                    settings,
                    database,
                    market=market,
                    publisher_factory=factory,
                    notices=notices,
                ).run()
            except Exception as exc:  # pragma: no cover — the orchestrator catches its own
                log.error(
                    "scheduler.scan_failed",
                    market=market.value,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            else:
                # M8: ARCHITECTURE §2's "alert after 3 consecutive cycle failures",
                # evaluated from the cycles table after every cycle — including the
                # ones that succeed, which is how the recovery notice gets sent. It
                # never raises; see sentinel/bot/alerts.py.
                if state.bot is not None:
                    await _alerter_for(state, settings, database, market).after_cycle(result)
            finally:
                state.last_cycle_at = utc_now()

        return scan

    def track_for(market: Market) -> Callable[[], Awaitable[None]]:
        """One tracker loop per enabled market (M10c).

        Until M10c this was a single job hard-wired to crypto, with a comment naming
        the day it would have to change. This is that day, and the reason the job is
        **per market** rather than one loop over markets is the same reason
        ``TrackerLoop`` takes a market at all: a ``PriceFeed`` wraps exactly one
        venue's adapter, so a loop can only follow signals it can price. One job that
        tried to do both would hold two adapters open on a 60-second timer and would
        fail both markets whenever either venue did.

        With forex disabled this registers exactly one job, and it is the one that has
        been running for 54 cycles.
        """

        async def track() -> None:
            try:
                async with _tracker_source(settings, database, market) as source:
                    loop = TrackerLoop(
                        database,
                        PriceFeed(
                            source,
                            settings.config.tracker,
                            max_candles=_candle_ceiling(settings, market),
                        ),
                        settings,
                        market=market,
                    )
                    result = await loop.tick()
                if state.bot is not None:
                    eligible = await _eligible_user_ids(database, now=utc_now())
                    await _notifier_for(state, settings, database, eligible).deliver()
                    # specs/TELEGRAM_UX.md §4's daily-loss notice. It goes to the user
                    # whose book hit the limit, not to the owner: the pause holds back
                    # that person's cards, and they have no /status to find out why.
                    await _announce_pauses(state, settings, database, result.paused_users)
            except Exception as exc:
                log.error(
                    "scheduler.tick_failed",
                    market=market.value,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        return track

    # Every id this function registers, in registration order, for the log line
    # below. Collected here rather than read back from `scheduler.get_jobs()`,
    # because the heartbeat job is registered before this function is called and
    # would silently join the list — this field is meant to say what the PIPELINE
    # registered, and nothing else.
    job_ids: list[str] = []

    for market in settings.config.enabled_markets:
        scan_id = f"scan:{market.value}"
        scheduler.add_job(
            scan_for(market),
            trigger="interval",
            minutes=settings.config.market(market).scan_interval_minutes,
            id=scan_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        job_ids.append(scan_id)
    for market in settings.config.enabled_markets:
        # The crypto job keeps the bare id ``tracker`` it has had since M7. Renaming it
        # would be a live-system change for tidiness: APScheduler keys on the id, and
        # this deployment's running job is that one.
        job_id = "tracker" if market is LEGACY_MARKET else f"tracker:{market.value}"
        scheduler.add_job(
            track_for(market),
            trigger="interval",
            seconds=schedule.tracker_interval_seconds,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        job_ids.append(job_id)

    if Market.FOREX in settings.config.enabled_markets:
        # §3 requirement 5, which had no caller until M10d. The access token lives
        # ~20 minutes, but the cadence is really about the REFRESH token: it lives
        # ~1 hour, rotates on every use, and each refresh resets that hour. That hour
        # is the whole margin between a restart that survives unattended and one that
        # needs a browser login, so this runs on its own timer rather than riding on a
        # scan — including all weekend, when the market is shut and nothing is reading
        # a chart. Without it the token would be touched once an hour by a 60-minute
        # scan, against a credential that lives about that long: survival by luck.
        #
        # `jitter` so a fleet of restarts does not synchronise on the token endpoint,
        # and `max_instances=1` because two concurrent refreshes would each spend the
        # same single-use token and one of them would lose.
        scheduler.add_job(
            _refresh_forex_token(state, settings, database),
            trigger="interval",
            seconds=settings.config.forex.token_refresh_interval_seconds,
            jitter=30,
            id=FOREX_TOKEN_JOB,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        job_ids.append(FOREX_TOKEN_JOB)

    log.info(
        "scheduler.pipeline_scheduled",
        markets=[market.value for market in settings.config.enabled_markets],
        # M10c §11 asserts crypto's tracker keeps the bare id ``tracker``, and that
        # assertion was checkable only from the test suite: APScheduler's own boot
        # line logs the FUNCTION name (``track_for.<locals>.track``), so the deploy
        # could not confirm it (journal/M10c_REPORT.md, Deployed §2). This puts the
        # ids where a human on the box can read them. On switch-on day there will be
        # two tracker jobs and the difference will matter.
        job_ids=job_ids,
        scan_interval_minutes={
            market.value: settings.config.market(market).scan_interval_minutes
            for market in settings.config.enabled_markets
        },
        tracker_interval_seconds=schedule.tracker_interval_seconds,
        dry_run={
            market.value: settings.config.market(market).dry_run
            for market in settings.config.enabled_markets
        },
    )


def _credential_keeper(
    state: AppState, settings: Settings, database: Database
) -> ForexCredentialKeeper:
    """The Saxo credential's keeper, with somewhere to send the re-auth alert.

    ``notices`` is ``None`` when no bot is configured. That is a degraded start, not a
    crash — /health and the scheduler are still useful — and the keeper then logs
    ``forex.reauth_required`` and sends nothing, which is the honest outcome rather
    than a swallowed exception.
    """
    notices = None if state.bot is None else _notices_for(state, settings, database)
    return ForexCredentialKeeper(settings, database, notices=notices)


def _refresh_forex_token(
    state: AppState, settings: Settings, database: Database
) -> Callable[[], Coroutine[Any, Any, None]]:
    async def refresh() -> None:
        # `tick()` never raises; this wrapper exists only to give APScheduler a
        # zero-argument coroutine and to keep the bot lookup late, so a bot that
        # starts after the scheduler is still found.
        await _credential_keeper(state, settings, database).tick()

    return refresh


@asynccontextmanager
async def _tracker_source(
    settings: Settings, database: Database, market: Market
) -> AsyncIterator[CandleSource]:
    """The candles this market's tracker prices from, closed after use (M10d).

    **This was the defect join 2 of journal/M10c_REPORT.md §13 existed to find.** Both
    markets went through :func:`~sentinel.core.wiring.market_adapter`, which supplies
    neither an HTTP fetcher nor an access-token provider — and ``wiring._saxo`` raises
    ``UnknownAdapter`` without both, because the Saxo OAuth chain is stateful,
    single-use and lives in Postgres (§3). Binance needs neither, so the crypto
    tracker has been fine since M7 and the forex one could never have started: the
    exception is swallowed by ``track()``'s own ``except Exception`` into a
    ``scheduler.tick_failed`` line, so ``tracker:forex`` would have died on every tick
    for ever while ``/health`` stayed green.

    Forex also needs :class:`~sentinel.fx.pricing.ForexCandleSource` rather than the
    adapter directly. That module says why; in one line, Saxo has no closed flag, so
    §4.1's clock rule is applied by the adapter and ``PriceFeed``'s two questions —
    "include the forming bar" for fills, "closed only" for invalidation — need
    answering separately rather than by one filter that is wrong for both.
    """
    if market is not Market.FOREX:
        async with market_adapter(settings, market) as adapter:
            yield adapter
        return
    async with forex_adapter(settings, database) as adapter:
        yield ForexCandleSource(adapter, settings.config.forex, settings.config.tracker)


def _candle_ceiling(settings: Settings, market: Market) -> int:
    """This venue's per-request candle ceiling.

    Binance's is 1000 and Saxo's is 1200, and Saxo **clamps silently** above it rather
    than erroring (spike defect D-e). A forex feed left on Binance's number would
    under-request by 200 bars — not wrong so much as arbitrary — and one that guessed
    higher would take a short read for a quiet market.
    """
    if market is Market.FOREX:
        return settings.config.forex.max_count
    return DEFAULT_MAX_CANDLES


def _publisher_factory(
    state: AppState, settings: Settings, database: Database, market: Market
) -> Callable[[int], SignalPublisher]:
    """One publisher per recipient (M8.1).

    A private chat id equals the user id on Telegram, so ``chat_ids`` is that one
    chat. What changed is not the delivery mechanism but what is delivered: each
    user's own plan, sized against their own capital, stored under their own
    ``user_id``.
    """
    assert state.bot is not None
    bot = state.bot.bot
    telegram = settings.config.telegram
    tz = zone_info(telegram.owner_timezone)

    def build(user_id: int) -> SignalPublisher:
        return SignalPublisher(
            database,
            bot,
            user_id=user_id,
            chat_ids=(user_id,),
            telegram=telegram,
            tz=tz,
            market=market,
            show_market=settings.config.multi_market,
        )

    return build


def _alerter_for(
    state: AppState, settings: Settings, database: Database, market: Market
) -> AdminAlerter:
    """Cycle failures, recovery and spend notices — **to the owner alone** (M8.1).

    A member has no lever to pull in response to a failed cycle or an LLM bill, and
    telling them the system is broken would be alarming without being actionable.
    """
    assert state.bot is not None
    owner_id = settings.secrets.owner_user_id
    return AdminAlerter(
        database,
        state.bot.bot,
        chat_ids=() if owner_id is None else (owner_id,),
        settings=settings,
        tz=zone_info(settings.config.telegram.owner_timezone),
        market=market,
    )


async def _eligible_user_ids(database: Database, *, now: datetime) -> tuple[int, ...]:
    """Who may be sent anything right now (M8.1).

    Read per tick rather than cached on ``AppState``: an approval, a suspension or a
    ``/leave`` must take effect on the next tick, not on the next restart.
    """
    async with database.session() as session:
        approved = await UserRepository(session).approved()
    return tuple(user.telegram_user_id for user in approved if user.eligible_for_signals(now))


def _notifier_for(
    state: AppState, settings: Settings, database: Database, chat_ids: tuple[int, ...]
) -> TrackerNotifier:
    """Tracker replies, to everyone eligible — each about their own signals only."""
    assert state.bot is not None
    return TrackerNotifier(
        database,
        state.bot.bot,
        chat_ids=chat_ids,
        telegram=settings.config.telegram,
    )


def _notices_for(state: AppState, settings: Settings, database: Database) -> UserNotifier:
    assert state.bot is not None
    return UserNotifier(database, state.bot.bot, telegram=settings.config.telegram)


async def _announce_pauses(
    state: AppState, settings: Settings, database: Database, user_ids: Sequence[int]
) -> None:
    """One daily-loss notice per newly paused user, at most once per UTC day."""
    if not user_ids:
        return
    notices = _notices_for(state, settings, database)
    at = utc_now()
    tz = zone_info(settings.config.telegram.owner_timezone)
    for user_id in user_ids:
        await notices.notice(
            user_id,
            key=loss_pause_key(user_id, at),
            text=loss_pause_card(limit_pct=settings.config.risk.daily_loss_limit_pct, at=at, tz=tz),
        )


async def _seed_forex_credential(state: AppState, settings: Settings, database: Database) -> None:
    """Put ``.env``'s bootstrap refresh token into Postgres, once, at boot (M10d).

    **Nothing called ``bootstrap()`` until now**, so ``SAXO_REFRESH_TOKEN`` never left
    ``.env``: the token store was empty on the first cycle, ``refresh()`` raised "no
    Saxo credential is stored", and forex would have been dead from its first minute
    with a perfectly good credential sitting in the environment. It is the earliest and
    quietest of the three unwired pieces of the OAuth chain (journal/M10d_REPORT.md P3).

    Idempotent by construction rather than by this call site remembering to be careful:
    ``bootstrap()`` refuses to overwrite a stored credential, because the ``.env`` token
    is single-use and was spent on the first refresh — so a redeploy that overwrote the
    live chain with it would end the chain and force the login it exists to avoid.

    Skipped entirely when forex is disabled, so a crypto-only boot does not touch a
    Saxo table or read a Saxo secret.
    """
    if Market.FOREX not in settings.config.enabled_markets:
        return
    await _credential_keeper(state, settings, database).seed()


async def _seed_owner(database: Database, settings: Settings) -> None:
    """Make sure an OWNER row exists (M8.1).

    Migration 0007 does this for a database that already had signals or settings to
    attribute. This covers the one it deliberately cannot: an empty database, where
    there was nothing to attribute and therefore no reason to fail.

    Idempotent and never an update, so it cannot re-approve an owner who suspended
    themselves or reset a capital they changed. Without ``TELEGRAM_OWNER_USER_ID`` the
    app still boots — ``/health``, the scheduler and the tracker are all useful — and
    says loudly that nobody can approve anybody until it is set.
    """
    owner_id = settings.secrets.owner_user_id
    if owner_id is None:
        log.warning(
            "app.no_owner_configured",
            detail="set TELEGRAM_OWNER_USER_ID — without it nobody can be approved "
            "and no admin alert has a destination",
        )
        return
    async with database.session() as session:
        seeded = await UserRepository(session).ensure_owner(
            owner_id, at=utc_now(), acknowledged_version=ACK_VERSION
        )
        await session.commit()
    log.info(
        "app.owner_ready",
        user_id=seeded.telegram_user_id,
        status=seeded.status.value,
        capital_set=seeded.capital_set,
    )


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

        if isinstance(db, Database):
            await _seed_owner(db, resolved)
            await _seed_forex_credential(state, resolved, db)
            state.last_cycle_at = await _last_cycle_at(db)
            _schedule_pipeline(scheduler, state, resolved, db)
        else:  # pragma: no cover — only an injected test double lands here
            log.warning("scheduler.pipeline_disabled", reason="no real database in this process")

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
            markets=[market.value for market in resolved.config.enabled_markets],
            watchlist_size={
                market.value: len(resolved.config.market(market).watchlist)
                for market in resolved.config.enabled_markets
            },
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
