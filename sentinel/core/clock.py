"""Single source of time. Everything internal is UTC (CLAUDE.md).

Owner-local time is applied only at Telegram render time (specs/DATA_SOURCES.md §4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


def utc_now() -> datetime:
    """Timezone-aware current time in UTC."""
    return datetime.now(UTC)


class Clock(Protocol):
    """Injectable clock so tests can freeze time."""

    def now(self) -> datetime: ...


class SystemClock:
    """Default :class:`Clock` backed by the system time."""

    def now(self) -> datetime:
        return utc_now()


class FrozenClock:
    """Test clock pinned to a fixed instant."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at
