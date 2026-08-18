"""Read models for the command cards.

A handler queries the database and assembles one of these; ``cards.py`` turns it
into text and does nothing else. Keeping the shapes here means a card renderer
never holds a session, and a test can render every command's output without one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class DataSourceView:
    """The freshness and quality of the newest snapshot for one symbol."""

    symbol: str
    quality: str
    captured_at: datetime
    degraded_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatusView:
    """``/status`` — only what M6 can actually measure (see ``status_card``)."""

    paused: bool
    pause_reason: str | None
    paused_until: datetime | None
    capital_eur: Decimal | None
    risk_per_trade_pct: Decimal
    watchlist_size: int
    signals_total: int
    signals_undecided: int
    signals_taken: int
    stuck_messages: int
    data_sources: tuple[DataSourceView, ...] = ()


@dataclass(frozen=True)
class SettingsView:
    """``/settings`` — grouped ``(label, value, source)`` rows, source-tagged."""

    groups: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = field(default=())


__all__ = ["DataSourceView", "SettingsView", "StatusView"]
