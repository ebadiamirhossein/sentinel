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
class UserView:
    """One row of ``/users`` — and a privacy boundary expressed as a type (M8.1).

    The owner needs to operate the system: who is waiting, who is set up, who is
    stuck, who is currently held by a loss pause. The owner does **not** need to
    watch anyone trade, and this milestone's owner ruling is that the two must not
    be conflated.

    So the boundary lives here rather than in the renderer's discretion: this view
    has **no field** for a capital amount, a risk %, a P&L figure, a win rate, or a
    decision. ``capital_set`` is a bool on purpose — "have they finished setting up"
    is an operational fact, "how much are they trading with" is not the owner's.
    A future card cannot quietly widen the boundary without adding a field here,
    which is a visible change with a test against it.
    """

    user_id: int
    #: ``@handle``, a display name, or the bare id — whatever identifies them.
    label: str
    status: str
    role: str
    #: When they were approved, or when they asked if they have not been.
    since: datetime | None
    since_label: str
    capital_set: bool
    loss_paused: bool


@dataclass(frozen=True)
class DataSourceView:
    """The freshness and quality of the newest snapshot for one symbol."""

    symbol: str
    quality: str
    captured_at: datetime
    degraded_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpendView:
    """``llm_calls`` totals for ``/status`` (M7's spend guard).

    ``is_floor`` is true when some call could not be priced, so the figure
    understates reality by an unknown amount and the card says "at least".
    """

    day_usd: Decimal
    month_usd: Decimal
    limit_usd: Decimal
    warn_usd: Decimal
    state: str
    is_floor: bool = False
    unpriced_calls: int = 0


@dataclass(frozen=True)
class AlertView:
    """One admin alert (M8). Everything already decided in ``core/alerts.py``.

    There is no alert format in specs/TELEGRAM_UX.md — this is an addition, not a
    deviation, recorded in journal/M8_REPORT.md §3. It is deliberately unlike a
    signal card: no buttons, no charts, no disclaimer. It is the system talking
    about itself, and it must be impossible to mistake for something to trade.
    """

    kind: str
    title: str
    body: tuple[str, ...] = ()
    at: datetime | None = None


@dataclass(frozen=True)
class StatusView:
    """``/status`` — pipeline health (specs/TELEGRAM_UX.md §3).

    Cycle timing, open-risk usage and spend arrived with M7's orchestrator and
    tracker; through M6 this view named them as not-yet-measured instead.
    """

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
    #: M7. ``None`` for last_cycle_at means no cycle has ever completed.
    dry_run: bool = False
    last_cycle_at: datetime | None = None
    last_cycle_status: str | None = None
    cycles_completed: int = 0
    cycles_started: int = 0
    open_risk_pct: Decimal = Decimal("0")
    max_open_risk_pct: Decimal = Decimal("0")
    open_positions: int = 0
    max_positions: int = 0
    signals_today: int = 0
    max_signals_per_day: int = 0
    signals_open: int = 0
    spend: SpendView | None = None
    #: M8.1 — which pause is holding. ``"system"`` is the operator's ``/pause`` and
    #: stops everybody; ``"you"`` is this caller's own daily-loss pause and stops
    #: only them. Two rails with the same effect and completely different causes, so
    #: the card says which one it is rather than leaving the owner to guess.
    pause_scope: str = ""


@dataclass(frozen=True)
class PositionView:
    """One ✅ Taken signal, marked to market (§3's "live uPnL in R and EUR").

    Every figure is computed in ``risk/accounting.py`` and arrives here already
    made: §1's rule is that the bot renders and never computes, and M6 deferred
    this card precisely because the arithmetic did not exist yet.
    """

    number: int
    symbol: str
    direction: str
    setup_type: str
    status: str
    avg_entry: str
    stop: str
    targets: tuple[str, ...]
    risk_eur: str
    leverage: str
    expires_at: datetime
    filled_pct: str
    tp_hits: int
    #: Present once at least one rung has filled.
    mark_price: str | None = None
    unrealized_r: str | None = None
    unrealized_eur: str | None = None
    realized_r: str | None = None
    realized_eur: str | None = None
    stop_moved_to: str | None = None


@dataclass(frozen=True)
class TrackerEventView:
    """One §4 reply, with every number already computed by the tracker."""

    kind: str
    symbol: str
    number: int
    price: str | None = None
    realized_r: str | None = None
    realized_eur: str | None = None
    detail: str = ""
    payload: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StatsGroupView:
    """One population's figures, pre-rendered (``/stats``)."""

    label: str
    note: str
    measured: bool
    count: int
    filled: int
    unfilled: int
    wins: int
    losses: int
    scratches: int
    win_rate_pct: str
    avg_r: str
    total_r: str
    total_eur: str
    costs_eur: str
    profit_factor: str
    max_drawdown_r: str
    reached_tp1: int
    reached_tp1_pct: str


@dataclass(frozen=True)
class StatsBreakdownView:
    key: str
    count: int
    win_rate_pct: str
    avg_r: str


@dataclass(frozen=True)
class StatsView:
    """``/stats [30d|90d|all]`` — three populations, never merged."""

    window: str
    since: datetime | None
    groups: tuple[StatsGroupView, ...] = ()
    by_setup: tuple[StatsBreakdownView, ...] = ()
    by_prompt_version: tuple[StatsBreakdownView, ...] = ()


@dataclass(frozen=True)
class SettingsView:
    """``/settings`` — grouped ``(label, value, source)`` rows, source-tagged."""

    groups: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = field(default=())


__all__ = [
    "AlertView",
    "DataSourceView",
    "PositionView",
    "SettingsView",
    "SpendView",
    "StatsBreakdownView",
    "StatsGroupView",
    "StatsView",
    "StatusView",
    "TrackerEventView",
    "UserView",
]
