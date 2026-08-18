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
    InstrumentMetaRepository,
    RiskStateRepository,
    RuntimeSettingsRepository,
    SignalRepository,
    SnapshotRepository,
    TelegramMessageRepository,
)


@dataclass(frozen=True)
class Repositories:
    """Which repository classes the handlers instantiate per session."""

    signals: type[SignalRepository] = SignalRepository
    messages: type[TelegramMessageRepository] = TelegramMessageRepository
    settings: type[RuntimeSettingsRepository] = RuntimeSettingsRepository
    risk_state: type[RiskStateRepository] = RiskStateRepository
    snapshots: type[SnapshotRepository] = SnapshotRepository
    instruments: type[InstrumentMetaRepository] = InstrumentMetaRepository


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
