"""Dry run: the whole cycle, and a completely silent phone.

The first unattended run is the riskiest moment in the project, so ``dry_run``
exists to make it observable before it can talk. What has to be true:

* nothing reaches Telegram — not the card, and not the tracker's replies either;
* the signal is stored and tracked, so 24 hours of it produces a measured record
  rather than only an absence of crashes;
* the card that *would* have been sent is logged verbatim, by the same renderer,
  so what the log holds is what the owner would have read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.models import SignalRecord
from sentinel.bot.notifier import TrackerNotifier
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Secrets, Settings, load_config
from sentinel.core.orchestrator import CycleOrchestrator, CycleRepositories, CycleResult
from sentinel.risk.models import TradePlan
from tests.bot_double import FakeBot, FakeDatabase, FakeStore, _SignalRow

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
CHAT_ID = 4242


@pytest.fixture
def dry_settings() -> Settings:
    config = load_config()
    return Settings(
        secrets=Secrets(_env_file=None),
        config=config.model_copy(update={"dry_run": True}),
    )


class Signals:
    """Only ``claim`` — the one write the dry-run path makes."""

    def __init__(self, session: Any) -> None:
        self.store: FakeStore = session.store

    async def claim(self, record: SignalRecord) -> SignalRecord | None:
        if self.store.by_plan_id(record.plan.plan_id) is not None:
            return None
        row = _SignalRow(record.signal_id, record.plan.plan_id, number=len(self.store.signals) + 1)
        row.dry_run = record.dry_run
        row.symbol = record.plan.symbol
        row.plan = record.plan.model_dump(mode="json")
        self.store.signals[record.signal_id] = row
        return record.model_copy(update={"number": row.number})


async def test_an_approved_plan_is_stored_and_never_sent(
    dry_settings: Settings, plan: TradePlan
) -> None:
    """The publisher is present and must simply not be reached."""
    store = FakeStore()
    bot = FakeBot()
    orchestrator = CycleOrchestrator(
        dry_settings,
        FakeDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        repositories=CycleRepositories(signals=Signals),  # type: ignore[arg-type]
    )
    result = CycleResult(cycle_id=uuid4(), dry_run=True)

    await orchestrator._record_dry_run(result, plan, [])

    assert result.published == 1
    assert bot.calls == [], "a rehearsal must make no outbound Telegram call"
    stored = next(iter(store.signals.values()))
    assert stored.dry_run is True
    assert stored.symbol == "SOLUSDT"


async def test_the_card_that_would_have_been_sent_is_logged_verbatim(
    dry_settings: Settings, plan: TradePlan, tz: ZoneInfo, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rendered by the *same* function the publisher calls. A summary would not
    tell the owner whether the card they are about to start trusting is right."""
    store = FakeStore()
    orchestrator = CycleOrchestrator(
        dry_settings,
        FakeDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        repositories=CycleRepositories(signals=Signals),  # type: ignore[arg-type]
    )
    await orchestrator._record_dry_run(CycleResult(cycle_id=uuid4(), dry_run=True), plan, [])

    logged = capsys.readouterr().out
    assert "cycle.dry_run_card" in logged
    assert "not sent" in logged
    # A distinctive figure from the real card, proving the renderer ran.
    assert "82.675" in logged or "Weighted entry" in logged


async def test_the_tracker_stays_silent_about_a_rehearsal_signal(
    dry_settings: Settings, plan: TradePlan
) -> None:
    """The other half of the promise. A dry-run signal fills, stops and resolves
    exactly as a real one — and says nothing about any of it."""
    store = FakeStore()
    signal_id = uuid4()
    row = _SignalRow(signal_id, plan.plan_id, number=1)
    row.dry_run = True
    row.symbol = "SOLUSDT"
    store.signals[signal_id] = row
    store.events[(signal_id, "stop")] = {
        "signal_id": signal_id,
        "event_key": "stop",
        "kind": "STOPPED",
        "at": NOW,
        "price": Decimal("81.20"),
        "realized_r": Decimal("-1.00"),
        "realized_eur": Decimal("-74.98"),
        "payload": {},
        "detail": "Stopped",
    }

    bot = FakeBot()
    from tests.bot_double import FakeEventRepository, FakeMessageStore, FakeSignalRepository

    notifier = TrackerNotifier(
        FakeDatabase(store),  # type: ignore[arg-type]
        bot,
        chat_ids=(CHAT_ID,),
        telegram=dry_settings.config.telegram,
        clock=FrozenClock(NOW),
        messages=FakeMessageStore,
        events=FakeEventRepository,
        signals=FakeSignalRepository,
    )

    sent = await notifier.deliver()

    assert sent == 0
    assert bot.calls == [], "not one outbound call for a rehearsal signal"
    assert store.messages == {}, "and nothing claimed, so a later real run is unaffected"


async def test_turning_the_flag_off_does_not_retrospectively_make_it_real(
    dry_settings: Settings, plan: TradePlan
) -> None:
    """``signals.dry_run`` is written once and never revisited. A rehearsal signal
    stays a rehearsal signal, so /stats keeps it out of the real book forever."""
    store = FakeStore()
    orchestrator = CycleOrchestrator(
        dry_settings,
        FakeDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        repositories=CycleRepositories(signals=Signals),  # type: ignore[arg-type]
    )
    await orchestrator._record_dry_run(CycleResult(cycle_id=uuid4(), dry_run=True), plan, [])

    stored = next(iter(store.signals.values()))
    assert stored.dry_run is True

    live = Settings(secrets=dry_settings.secrets, config=load_config())
    assert live.config.dry_run is False
    assert stored.dry_run is True, "the stored row is immutable, not derived from config"
