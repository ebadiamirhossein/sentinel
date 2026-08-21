"""One scan job per enabled market — FOREX.md build-order step 8.

M10a wrote the loop; nothing pinned it. With forex disabled the deployment must
register exactly one ``scan:crypto`` job on exactly the trigger it has today, or
"deploying M10b is a no-op" is a claim rather than a fact.

The non-vacuity sibling enables forex in a test-only config and asserts a second job
appears, because a loop that registered one job unconditionally would pass the first
test just as well.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sentinel.core.app import AppState, _schedule_pipeline
from sentinel.core.config import Settings
from sentinel.core.markets import Market


def jobs_for(settings: Settings) -> dict[str, float]:
    """Job id -> interval in seconds, from a real scheduler that is never started."""
    scheduler = AsyncIOScheduler(timezone=UTC)
    state = AppState(
        settings=settings,
        database=None,  # type: ignore[arg-type]
        scheduler=scheduler,
    )
    _schedule_pipeline(scheduler, state, settings, None)  # type: ignore[arg-type]
    return {job.id: job.trigger.interval.total_seconds() for job in scheduler.get_jobs()}


def enable_forex(settings: Settings) -> Settings:
    config = settings.config.model_copy(deep=True)
    config.markets[Market.FOREX] = config.markets[Market.FOREX].model_copy(update={"enabled": True})
    return settings.model_copy(update={"config": config})


def test_with_forex_disabled_exactly_one_scan_job_is_registered_at_sixty_minutes(
    settings: Settings,
) -> None:
    """The no-op claim, pinned. One scan, one tracker, nothing else."""
    registered = jobs_for(settings)
    scans = {name: seconds for name, seconds in registered.items() if name.startswith("scan:")}
    assert scans == {"scan:crypto": 3600.0}
    assert registered["tracker"] == 60.0
    assert set(registered) == {"scan:crypto", "tracker"}


def test_a_second_enabled_market_gets_its_own_job_rather_than_sharing_one(
    settings: Settings,
) -> None:
    """The non-vacuity sibling, and the reason it is a job per market rather than one
    job that loops: a forex scan that overran must not delay a crypto scan, and each
    market keeps its own ``scan_interval_minutes`` and its own ``max_instances=1``."""
    registered = jobs_for(enable_forex(settings))
    scans = {name: seconds for name, seconds in registered.items() if name.startswith("scan:")}
    assert scans == {"scan:crypto": 3600.0, "scan:forex": 3600.0}


def test_each_scan_job_runs_alone_and_coalesces(settings: Settings) -> None:
    """A scan that overran its interval must not start a second copy of itself, and
    the missed runs must collapse rather than queue up behind it."""
    scheduler = AsyncIOScheduler(timezone=UTC)
    state = AppState(settings=settings, database=None, scheduler=scheduler)  # type: ignore[arg-type]
    _schedule_pipeline(scheduler, state, settings, None)  # type: ignore[arg-type]
    for job in scheduler.get_jobs():
        assert job.max_instances == 1
        assert job.coalesce is True


@pytest.mark.parametrize("market", [Market.CRYPTO, Market.FOREX])
def test_a_markets_scan_interval_is_its_own(settings: Settings, market: Market) -> None:
    """Per market because the two would disagree: a forex cycle is skipped for 49
    hours a week and a crypto one never is."""
    config = enable_forex(settings).config.model_copy(deep=True)
    config.markets[market] = config.markets[market].model_copy(update={"scan_interval_minutes": 17})
    tweaked = settings.model_copy(update={"config": config})
    assert jobs_for(tweaked)[f"scan:{market.value}"] == 17 * 60.0
