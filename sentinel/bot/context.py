"""Everything a handler needs, injected once instead of imported ad hoc.

aiogram passes workflow data into every handler, so a handler signature stays
``(message, ctx)`` and a test can build a context around a stub database without
touching module state.

``repositories`` is the same seam the publisher carries, for the same reason: the
handlers' behaviour — range validation, confirmation wording, the loss-limit
resume path — is worth testing on every run, not only when a developer happens to
have ``SENTINEL_TEST_DATABASE_URL`` set. The fakes subclass the real repositories,
so a signature that drifts fails ``mypy --strict`` rather than silently diverging.
``tests/bot/test_persistence.py`` exercises the real ones against real Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sentinel.bot.runtime import SymbolChecker
from sentinel.core.clock import Clock
from sentinel.core.config import Settings
from sentinel.storage.db import Database
from sentinel.storage.repositories import (
    AnalystReportRepository,
    CycleRepository,
    GateDecisionRepository,
    InstrumentMetaRepository,
    LLMCallRepository,
    RiskStateRepository,
    RuntimeSettingsRepository,
    SignalEventRepository,
    SignalExitRepository,
    SignalFillRepository,
    SignalRepository,
    SnapshotRepository,
    TelegramMessageRepository,
    UserRepository,
    WatchlistRequestRepository,
)


@dataclass(frozen=True)
class Repositories:
    """Which repository classes the handlers instantiate per session."""

    signals: type[SignalRepository] = SignalRepository
    messages: type[TelegramMessageRepository] = TelegramMessageRepository
    settings: type[RuntimeSettingsRepository] = RuntimeSettingsRepository
    #: M8.1 — read on *every* update by the auth middleware, so it is the one
    #: repository the bot cannot function without.
    users: type[UserRepository] = UserRepository
    risk_state: type[RiskStateRepository] = RiskStateRepository
    snapshots: type[SnapshotRepository] = SnapshotRepository
    instruments: type[InstrumentMetaRepository] = InstrumentMetaRepository
    watchlist_requests: type[WatchlistRequestRepository] = WatchlistRequestRepository
    # M7: the tracker's tables, read by /positions and by the notifier.
    fills: type[SignalFillRepository] = SignalFillRepository
    exits: type[SignalExitRepository] = SignalExitRepository
    events: type[SignalEventRepository] = SignalEventRepository
    cycles: type[CycleRepository] = CycleRepository
    llm_calls: type[LLMCallRepository] = LLMCallRepository
    # M8.4: /pulse reads the pipeline's own record. Read-only — these two are
    # written by the orchestrator and never by a handler.
    reports: type[AnalystReportRepository] = AnalystReportRepository
    gate_decisions: type[GateDecisionRepository] = GateDecisionRepository


@dataclass(frozen=True)
class BotContext:
    settings: Settings
    database: Database
    clock: Clock
    tz: ZoneInfo
    #: Confirms a new watchlist symbol exists. ``None`` disables the check — the
    #: edit still goes through, and ``/watchlist`` says the symbol was unverified.
    symbol_checker: SymbolChecker | None = None
    repositories: Repositories = field(default_factory=Repositories)


__all__ = ["BotContext", "Repositories"]
