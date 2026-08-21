"""One scan job per enabled market — FOREX.md build-order step 8.

M10a wrote the loop; nothing pinned it. With forex disabled the deployment must
register exactly one ``scan:crypto`` job on exactly the trigger it has today, or
"deploying M10b is a no-op" is a claim rather than a fact.

The non-vacuity sibling enables forex in a test-only config and asserts a second job
appears, because a loop that registered one job unconditionally would pass the first
test just as well.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC
from typing import Any

import pytest
import structlog
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


def scheduled_log_for(settings: Settings) -> MutableMapping[str, Any]:
    """The single ``scheduler.pipeline_scheduled`` event, as structlog saw it."""
    scheduler = AsyncIOScheduler(timezone=UTC)
    state = AppState(
        settings=settings,
        database=None,  # type: ignore[arg-type]
        scheduler=scheduler,
    )
    with structlog.testing.capture_logs() as captured:
        _schedule_pipeline(scheduler, state, settings, None)  # type: ignore[arg-type]
    events = [e for e in captured if e["event"] == "scheduler.pipeline_scheduled"]
    assert len(events) == 1, events
    return events[0]


def enable_forex(settings: Settings) -> Settings:
    config = settings.config.model_copy(deep=True)
    config.markets[Market.FOREX] = config.markets[Market.FOREX].model_copy(update={"enabled": True})
    return settings.model_copy(update={"config": config})


def disable_forex(settings: Settings) -> Settings:
    """Forex off — what the shipped config carried until M10d switched it on.

    The single-market assertions below are about **one enabled market**, not about
    which one the shipped file happens to enable this month. Reading the shipped flag
    made them a test of the config; against an explicit one they keep saying what they
    meant, and they keep the crypto-only job list pinned for the day forex is turned
    back off.
    """
    config = settings.config.model_copy(deep=True)
    config.markets[Market.FOREX] = config.markets[Market.FOREX].model_copy(
        update={"enabled": False}
    )
    return settings.model_copy(update={"config": config})


def test_with_forex_disabled_exactly_one_scan_job_is_registered_at_sixty_minutes(
    settings: Settings,
) -> None:
    """One scan, one tracker, nothing else — and **no forex token job**.

    That last clause is what makes this worth keeping after switch-on: M10d adds a
    five-minute credential-refresh job, and it must appear only when forex is enabled
    or a crypto-only deployment starts polling a token endpoint it has no keys for.
    """
    registered = jobs_for(disable_forex(settings))
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


# --------------------------------------------------------------------------- #
# One tracker per enabled market (M10c)
# --------------------------------------------------------------------------- #


def test_a_second_enabled_market_gets_its_own_tracker_loop(settings: Settings) -> None:
    """The gap ``app.track`` named as M10c's, closed.

    A job per market rather than one loop over markets, and for the reason
    ``TrackerLoop`` takes a market at all: a ``PriceFeed`` wraps exactly one venue's
    adapter, so a loop can only follow signals it can price. One job doing both would
    hold two adapters open on a 60-second timer and fail both markets whenever either
    venue did.
    """
    registered = jobs_for(enable_forex(settings))
    trackers = {name: seconds for name, seconds in registered.items() if name.startswith("tracker")}

    assert trackers == {"tracker": 60.0, "tracker:forex": 60.0}


def test_the_crypto_tracker_keeps_the_bare_job_id_it_has_always_had(settings: Settings) -> None:
    """APScheduler keys on the id, and the running deployment's job is ``tracker``.

    Renaming it to ``tracker:crypto`` for symmetry would be a live-system change made
    for tidiness — the kind this milestone exists not to make.
    """
    assert "tracker" in jobs_for(settings)
    assert "tracker:crypto" not in jobs_for(enable_forex(settings))


# --------------------------------------------------------------------------- #
# The ids, where a human on the box can read them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("multi", [False, True])
def test_the_boot_log_names_every_job_it_registered(settings: Settings, multi: bool) -> None:
    """``job_ids`` must equal what was actually registered, in both shapes.

    Added 2026-08-21 (hygiene session).
    ``test_the_crypto_tracker_keeps_the_bare_job_id_it_has_always_had`` above pins the
    id itself, but that assertion was checkable **only** from this
    suite: APScheduler's own boot line logs the function name —
    ``_schedule_pipeline.<locals>.track_for.<locals>.track`` — so the M10c deploy
    could not confirm it from the server at all (journal/M10c_REPORT.md, Deployed
    §2). This is what makes it readable there.

    Asserted against ``scheduler.get_jobs()`` rather than against a literal, so the
    log cannot drift from what was registered — a hand-written list would be a second
    place for the ids to be wrong, which is the failure it exists to prevent. The
    heartbeat job is deliberately absent: it is registered by the lifespan before this
    function runs, and this field describes the pipeline.
    """
    target = enable_forex(settings) if multi else settings

    logged = scheduled_log_for(target)
    ids = logged["job_ids"]
    assert isinstance(ids, list)

    # Set equality plus a duplicate check, not list equality: the ORDER is pinned by
    # the literal test below, and comparing lists here would couple this to
    # APScheduler's `get_jobs()` ordering, which is its business and not ours.
    assert sorted(ids) == sorted(jobs_for(target))
    assert len(ids) == len(set(ids))


def test_the_logged_ids_are_the_ones_switch_on_day_will_read(settings: Settings) -> None:
    """The literal, once, because the relationship test above cannot see a rename.

    If both the registration and the log moved together, the test above would still
    pass. This is the one place the actual strings are written down, and the bare
    ``tracker`` is the whole point of it.
    """
    assert scheduled_log_for(disable_forex(settings))["job_ids"] == ["scan:crypto", "tracker"]
    assert scheduled_log_for(enable_forex(settings))["job_ids"] == [
        "scan:crypto",
        "scan:forex",
        "tracker",
        "tracker:forex",
        # M10d. §3 requirement 5's five-minute cadence, which had no caller at all
        # until this milestone. Registered ONLY with forex enabled, which is what
        # keeps the crypto-only list above unchanged — the "deploying this is a no-op"
        # claim rests on that line, not on an argument.
        "forex-token-refresh",
    ]
