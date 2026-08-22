"""ORM models — the first real schema (M1).

Shape chosen with the owner: candles are normalized and upserted so a repeated
cycle does not rewrite 700 rows per symbol, while the per-cycle context that is
genuinely snapshot-shaped (funding, book, news, sentiment, FX, degradation) is
stored as JSONB on the snapshot row. A signal stays fully reconstructable from
the DB alone (PRD F10).

Money-ish columns are ``Numeric`` and map to ``Decimal`` — never float.

**M10a adds a ``market`` column** to every table whose rows belong to exactly one
market, defaulting to ``crypto`` and backfilled there by migration 0010. Four kinds
of table deliberately do *not* get one, and the distinction is worth stating once
rather than defending ten times:

* ``signal_fills``, ``signal_exits``, ``signal_events`` and ``telegram_messages``
  hang off a ``signal_id``, and that signal already carries the market. A copy would
  be a second place for the answer to be wrong.
* ``fx_rates`` holds EUR→USD, which belongs to the *display* layer and is read by
  every market alike.
* ``users``, ``runtime_settings`` and ``config_changes`` are about people and
  settings, not about instruments.
* ``risk_state`` stays the **global** operator pause; the per-market one is
  :class:`MarketPauseStateRow`, for the reasons recorded there.

``ohlcv_candles`` keeps its ``(symbol, timeframe, open_time)`` primary key and gains
``market`` as a plain column. Symbol strings are disjoint across markets today
(``BTCUSDT`` vs ``EURUSD``), so the key is still sound and the migration stays cheap
on the largest table in the schema — but it is an assumption, not a guarantee, and
journal/M10a_REPORT.md flags it for M10b.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Identity,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.core.markets import LEGACY_MARKET, MARKET_COLUMN_LENGTH
from sentinel.storage.base import Base

#: Wide enough for any crypto price or notional without losing precision.
PRICE = Numeric(38, 18)


class MarketSnapshotRow(Base):
    """One symbol, one cycle: the analyst's full input, minus the candle series."""

    __tablename__ = "market_snapshots"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    cycle_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    last_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    data_quality: Mapped[str] = mapped_column(String(16), nullable=False)
    degraded_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    #: Derivatives, order book, news, sentiment, macro, FX and instrument meta.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: {field: {source, fetched_at}} — provenance for every part of the snapshot.
    sources: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_market_snapshots_symbol_captured_at", "symbol", "captured_at"),
        Index("ix_market_snapshots_cycle_id", "cycle_id"),
    )


class OhlcvCandleRow(Base):
    """Deduplicated candle history, upserted every cycle.

    Keyed by (symbol, timeframe, open_time) so re-fetching the same window is
    idempotent — and so M9's backtest harness has a clean series to replay.
    """

    __tablename__ = "ohlcv_candles"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )
    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # No extra index: the (symbol, timeframe, open_time) primary key already
    # serves both the upsert conflict target and range scans by symbol.


class InstrumentMetaRow(Base):
    """Exchange trading rules, cached 24h (specs/DATA_SOURCES.md §2.1)."""

    __tablename__ = "instrument_meta"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )
    tick_size: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    qty_step: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    min_notional: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    contract_size: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("1"))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ForexInstrumentRow(Base):
    """Resolved Saxo FxSpot instruments and their **cross-checked** pip (M10b).

    A table of its own rather than rows in ``instrument_meta``, and that is a
    correction to specs/FOREX.md §4.2 rather than a convenience. ``instrument_meta``
    is a tick size, a quantity step, a minimum **notional** and a contract size —
    Binance's shape. Forex has a Uic, a ``Format.Decimals``, a pip and a minimum
    **trade size** in base units, and no lot rounding at all. Three of those have no
    column here and one of them (a units figure landing in ``min_notional``) would
    read as money. Same defect class as ``TradePlan`` not fitting a forex plan.

    ``pip`` is stored even though it is derivable from ``decimals``, so the value the
    system actually used is on the record. The whole of failure mode A is that a
    plausible pip and a plausible ``decimals`` can disagree by a factor of ten
    without anything saying so; a stored pip makes that auditable after the fact.
    """

    __tablename__ = "forex_instruments"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    uic: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: ``Format.Decimals`` — the **pip** precision, not the quote precision.
    decimals: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pip: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    min_trade_size: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    amount_decimals: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SaxoTokenRow(Base):
    """The rotating Saxo OAuth credential (M10b, specs/FOREX.md §3).

    **One row, enforced by the schema.** ``id`` is fixed at 1: a second row would be
    a second answer to "which refresh token is live", and since the refresh token is
    single-use the wrong answer is not merely stale, it is spent.

    In Postgres rather than a file because §3 requirement 6 needs it to survive a
    restart *and* a redeploy, and because the rotated token must be written inside
    the same transaction that used it.

    Values are stored in the clear here for the same reason the database holds every
    other operational secret it is given — the deployment's protection is that
    Postgres publishes no host port at all. Nothing reads these columns into a log
    line: :mod:`sentinel.ingestion.clients.saxo_auth` logs sha256 prefixes only.

    Both expiries are **nullable**, and that is meaningful rather than lazy: a
    bootstrap refresh token pasted in from a manual browser login has no lifetime we
    were told, and inventing one would be exactly the "read every operational value,
    never assume" failure that spike defect D-c is about. Unknown stays unknown until
    the first refresh answers it.
    """

    __tablename__ = "saxo_oauth_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    access_token: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_token: Mapped[str] = mapped_column(String(2048), nullable=False)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    obtained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: How many times the chain has rotated. Operational evidence, not a control:
    #: a counter that stops climbing is the visible symptom of a persist that is
    #: silently failing, which is the bug §3 requirement 2 exists to catch.
    refresh_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class FxRateRow(Base):
    """Last-known-good FX rate, so a restart during an outage still has one (§2.4)."""

    __tablename__ = "fx_rates"

    pair: Mapped[str] = mapped_column(String(16), primary_key=True)
    rate: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GateDecisionRow(Base):
    """Every risk-gate verdict, approved or not (PRD F10, G5).

    The ``reason`` column holds a machine-readable ``RejectionReason`` — M9 asks
    "what is the gate rejecting most often, and was it right to?", which a prose
    message cannot answer.

    From M8.1 there is one row per (cycle, symbol, **user**): the analysis is
    shared but the sizing and the rails are not, so the same report can approve for
    one user and reject with ``MAX_OPEN_RISK`` for another. Dropping the user would
    make that pair of verdicts contradict each other in the audit trail.
    """

    __tablename__ = "gate_decisions"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    cycle_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gate_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    prompt_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The full TradePlan on approval, null otherwise.
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_gate_decisions_symbol_evaluated_at", "symbol", "evaluated_at"),
        Index("ix_gate_decisions_reason", "reason"),
    )


class RiskStateRow(Base):
    """Single-row pause state (§7): a pause must survive a restart."""

    __tablename__ = "risk_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pause_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MarketPauseStateRow(Base):
    """One market's operator pause (M10a). ``risk_state`` stays the *global* one.

    Deliberately a second table rather than a ``market`` column on ``risk_state``.
    A pause has a scope, and "all markets" is not a market: giving ``risk_state`` a
    market column would have forced a sentinel value like ``'global'`` into a column
    typed as a :class:`~sentinel.core.markets.Market`, and every reader would then
    have had to remember that one value in that column is not really a market.

    Splitting them also means **the live global pause row is not migrated at all**.
    ``/pause`` with no argument still writes exactly the row it wrote yesterday, so
    the one rail the owner reaches for in an emergency has no new code path in it.

    Mirrors ``risk_state`` and ``users`` field for field, so one ``PauseState``
    helper round-trips all three.
    """

    __tablename__ = "market_pause_state"

    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH), primary_key=True, autoincrement=False
    )
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pause_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserMarketPauseRow(Base):
    """One user's daily-loss pause **in one market** (M10a).

    ``users.paused``/``pause_reason``/``paused_until`` keep the meaning they have
    had since M8.1 and are not migrated: they are now explicitly the user's
    *combined-across-markets* loss pause, which is the rail that fires when the day
    is bad everywhere rather than in one place. This table is the per-market half.

    Both exist because they answer different questions. A forex loss must stop forex
    without stopping a crypto book that is having a fine day (M10a Step 5); a loss
    that is only bearable because it is spread across two markets must still stop
    everything. One column could not hold both, and collapsing them would silently
    pick whichever answer the last writer had.

    No foreign key to ``users``, matching every other table here.
    """

    __tablename__ = "user_market_pauses"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH), primary_key=True, autoincrement=False
    )
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pause_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_user_market_pauses_user_id", "user_id"),)


class WatchlistRequestRow(Base):
    """A member asking for a symbol to join the shared watchlist (M8.3).

    The watchlist decides what the deep analyst is pointed at, and an analyst call
    is ~$0.28 on the owner's key. So a member cannot edit it: they ask, and the
    owner approves. This table is the asking.

    **One PENDING request per symbol is a database guarantee**, not a check in a
    handler — ``uq_watchlist_requests_one_pending`` is a partial unique index over
    ``symbol WHERE status = 'PENDING'``, so a duplicate is an ``ON CONFLICT DO
    NOTHING`` that creates nothing and notifies nobody. Same reasoning as
    ``uq_users_single_owner`` (M8.1): two people asking for LINKUSDT within a second
    of each other must not produce two cards, and a counter or a pre-check would be
    a race.

    Decided rows are **kept**, and the index is partial precisely so they can be:
    a rejected symbol may be asked for again later, because the reason to hold a
    symbol is a fact about the market and markets change. Deleting the history
    instead would make "has anyone asked for this before" unanswerable.

    No foreign keys, matching every other table here.
    """

    __tablename__ = "watchlist_requests"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )
    requested_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: PENDING | APPROVED | REJECTED
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        # Per market from M10a. Without ``market`` in the key, a pending EURUSD
        # request would block a pending EURUSD request on the *other* market — and,
        # worse, the block would be silent, because a duplicate is an ON CONFLICT DO
        # NOTHING that notifies nobody. Symbols are disjoint across markets today,
        # which is exactly the kind of accident that stops being true quietly.
        Index(
            "uq_watchlist_requests_one_pending",
            "market",
            "symbol",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_watchlist_requests_status", "status"),
    )


class IngestionFailureRow(Base):
    """Why a symbol was skipped or a source degraded — auditability (PRD G5)."""

    __tablename__ = "ingestion_failures"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("cycle_id", "symbol", "source", name="uq_ingestion_failures_cycle"),
    )


class LLMCallRow(Base):
    """Every LLM request/response, successful or not (PRD F10, G5; CLAUDE.md).

    Failures are rows too. M9 asks "how often does the analyst return invalid
    JSON, and does that correlate with a prompt version?" — a table holding only
    successes cannot answer it, and the JSON-validity target in PRD §6 is
    literally a ratio over this table.

    ``request`` holds text blocks verbatim and images as ``{sha256, params}``
    references: a chart re-renders byte-identically from stored OHLCV (M3), so
    reconstruction holds without ~400KB of PNG per analyst call.
    """

    __tablename__ = "llm_calls"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    cycle_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    #: Null for the screener — it is one batch call across the whole watchlist.
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    tokens_in: Mapped[int] = mapped_column(nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    #: Derived from token counts and config pricing. An estimate, by name.
    cost_usd_estimate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("0")
    )
    duration_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        #: The spend guard's own query, and from M10a it is per market: each market
        #: has its own daily sub-budget under a shared ceiling (Step 4), so "what has
        #: this market spent today" has to be an index scan rather than a filter over
        #: the whole day's calls.
        Index("ix_llm_calls_market_started_at", "market", "started_at"),
        Index("ix_llm_calls_symbol_started_at", "symbol", "started_at"),
        # M9 groups by (prompt_version, status) to compute JSON validity per
        # prompt version, and by (model, started_at) for the M8 spend guard.
        Index("ix_llm_calls_prompt_version_status", "prompt_version", "status"),
        Index("ix_llm_calls_cycle_id", "cycle_id"),
    )


class AnalystReportRow(Base):
    """One validated analyst report (ARCHITECTURE.md §3 contract 3).

    ``role`` exists from day one even though M5 only ever writes ``'primary'``:
    specs/ENSEMBLE.md §3 stores the GPT second opinion here with ``role='shadow'``
    at M10, and a column added now costs nothing while a migration then would
    have to rewrite live rows.

    Feeds specs/PROMPTS.md §3's history block: `recent_for_symbol` is the query.
    """

    __tablename__ = "analyst_reports"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    cycle_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    snapshot_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    llm_call_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: "primary" in M5; "shadow" for the M10 second opinion.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="primary")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)

    #: Broken out of the JSONB so /stats can group without a JSON path.
    candidate_status: Mapped[str] = mapped_column(String(16), nullable=False)
    setup_type: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[int] = mapped_column(nullable=False, default=0)
    thesis: Mapped[str] = mapped_column(String(1024), nullable=False, default="")

    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_analyst_reports_symbol_created_at", "symbol", "created_at"),
        Index("ix_analyst_reports_prompt_version", "prompt_version"),
        Index("ix_analyst_reports_setup_type", "setup_type"),
        Index("ix_analyst_reports_cycle_id", "cycle_id"),
    )


class SignalRow(Base):
    """A gate-approved plan that was (or is about to be) delivered to Telegram.

    ARCHITECTURE.md §3 contract 5 — "TradePlan + Telegram message ids + user
    decision + tracked outcome" — landing at M6 because idempotent posting and a
    persisted button state both need a row to hang off. The tracker's half arrives
    at M7 and only adds columns: ``status`` already carries the initial state of
    the state machine in ARCHITECTURE §3, so M7 extends the vocabulary rather than
    reshaping the table.

    ``plan_id`` is unique and is the idempotency key. It comes from the
    ``TradePlan`` itself, so re-publishing the same decision — after a restart, a
    retry, or a duplicated cycle — collides here instead of posting a second card.

    The whole plan is kept as JSONB rather than exploded into columns: §7 requires
    a signal's sizing to stay immutable after ``/capital`` changes, and the surest
    way to promise that is to store exactly what was shown to the owner.
    """

    __tablename__ = "signals"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: The ``TradePlan.plan_id`` — unique, and the reason a restart cannot double-post.
    plan_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    cycle_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    #: Whose signal this is (M8.1). One shared ``AnalystReport`` yields one row per
    #: approved user, each with its own sizing in ``plan``, its own ``decision`` and
    #: its own realized R — which is what makes every statistic downstream
    #: partitioned by construction rather than by a filter somebody has to remember.
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: Human-facing sequence, so a card can say "Signal #142" without exposing a
    #: UUID. A Postgres IDENTITY rather than a count(*): the number must be stable
    #: once printed on a card, and a count changes if a row is ever deleted.
    number: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    setup_type: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[int] = mapped_column(nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: ARCHITECTURE §3's tracker state machine. M6 only ever writes PENDING_ENTRY.
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING_ENTRY")
    #: TAKEN | WATCHING | SKIPPED — null until the owner presses a button.
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: The full TradePlan, Decimals as strings (``model_dump(mode="json")``).
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: ChartRenderParams per attached chart. M3 §7: charts have no life independent
    #: of the signal, so they live here rather than in a table of their own.
    chart_params: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)

    # ---- M7: the tracker's half (additive, as 0005 promised) ----------------
    #: Produced by a cycle running with ``dry_run: true``. Never published, still
    #: tracked, and reported by /stats as its own population — folding paper
    #: results into the numbers the owner will later compare against would be the
    #: exact dishonesty the milestone exists to prevent. Immutable: turning the
    #: flag off does not retrospectively make these signals real.
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Rolled up from ``signal_fills`` / ``signal_exits`` so the common reads
    #: (/positions, /stats, the rails) need one row rather than three queries. The
    #: leg tables stay the source of truth; these are derived and recomputed.
    filled_qty: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: Where the stop stands *now* — §5 moves it to breakeven after TP1, so the
    #: plan's original stop is no longer the live one.
    stop_price_current: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    tp_hits: Mapped[int] = mapped_column(nullable=False, default=0)

    #: Realized R, gross of costs — the figure §4 of TELEGRAM_UX prints. Costs are
    #: reported beside it rather than baked in, so a card reconciles by hand.
    realized_r: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    realized_eur: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: What the trade actually cost (M5.1 §10) — not the plan's estimate.
    realized_costs_eur: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)

    #: TP1 | TP2 | TP3 | STOP | INVALIDATION | EXPIRY | MANUAL — how it ended.
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    first_fill_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The instant the tracker last looked. Bounds the next tick's candle window,
    #: and is what makes a restart resume rather than re-scan.
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_signals_plan_id"),
        Index("ix_signals_symbol_created_at", "symbol", "created_at"),
        Index("ix_signals_status", "status"),
        Index("ix_signals_decision", "decision"),
        #: The tracker's own query: everything not in a terminal state.
        Index("ix_signals_status_expires_at", "status", "expires_at"),
        #: /stats windows every population by when the signal resolved.
        Index("ix_signals_closed_at", "closed_at"),
        #: Every per-user query — the rails, /positions, /stats — starts here, and
        #: from M10a every one of them is also scoped to a market. Leading with
        #: ``market`` rather than adding a second index: the market is always known
        #: at the call site, and two overlapping indexes on the same hot table would
        #: cost every insert for a read nothing performs.
        Index("ix_signals_market_user_id_created_at", "market", "user_id", "created_at"),
    )


class TelegramMessageRow(Base):
    """One row per message the bot owns — the whole of specs/TELEGRAM_UX.md §6.

    "All messages idempotent (message ids stored; restarts never double-post)" is
    this unique constraint. A row is claimed as ``PENDING`` *before* the send and
    updated to ``SENT`` after, so a crash in between leaves evidence rather than a
    silent gap: the claim is never re-sent (a duplicate card is worse than a
    missing one the owner can re-request), and ``/status`` reports the stuck row.
    """

    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    #: charts | card | update — one of each per signal per chat, per ``event_key``.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: M7. A signal's thread carries many updates — three fills, ladder-complete,
    #: three TP hits, a stop, an expiry, a manual close — and ``kind`` alone allowed
    #: exactly one. This is the event's identity within the kind ("fill:1", "tp:2",
    #: "stop", "decision_ack").
    #:
    #: NOT NULL with a ``''`` default rather than nullable, and that is the whole
    #: point: Postgres treats two NULLs as *distinct* in a unique index, so a
    #: nullable column would have quietly un-guaranteed the card's own idempotency
    #: — the guarantee this table exists for.
    event_key: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Null while the claim is PENDING; set when Telegram confirms the send.
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "signal_id", "kind", "chat_id", "event_key", name="uq_telegram_messages_signal_id"
        ),
        Index("ix_telegram_messages_status", "status"),
    )


class RuntimeSettingRow(Base):
    """Current value of one runtime-overridable setting (ARCHITECTURE.md §2).

    "Precedence: DB > yaml > defaults" — this table is the DB layer, and until M6
    it did not exist, so ``load_config``'s ``db_overrides`` parameter had no source.
    ``capital_eur`` in particular lives nowhere else: config.yaml deliberately omits
    it and the gate rejects with ``NO_CAPITAL`` until ``/capital`` writes it here.

    Values are JSONB so a Decimal, an int and a watchlist all fit one table without
    a column per setting. Decimals are stored as strings, as everywhere else.
    """

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ConfigChangeRow(Base):
    """Append-only audit of every runtime-config change (PRD F10).

    F10 requires Postgres to hold "config changes", not merely current values: when
    M9 asks why a signal was sized against €8,000, the answer has to be a row with a
    timestamp, not an inference. Kept separate from ``runtime_settings`` so the
    current value stays a cheap primary-key lookup.
    """

    __tablename__ = "config_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_config_changes_key_changed_at", "key", "changed_at"),)


# --------------------------------------------------------------------------- #
# M8.1 — who the users are
# --------------------------------------------------------------------------- #


class UserRow(Base):
    """One Telegram id and where it stands (specs/TELEGRAM_UX.md §7).

    Through M8 identity was ``TELEGRAM_ALLOWED_USER_IDS`` — one env var used as
    *both* the authorization list and the broadcast list, which works only for a
    single person. This table replaces it as the runtime authority; the env var
    survives to name the owner and to bootstrap this row.

    Two columns are here rather than in ``runtime_settings`` on purpose. ``capital_eur``
    and ``risk_per_trade_pct`` were one global value; under multiple users they are
    precisely the two numbers that must not be shared, and every plan is sized from
    them. ``None`` means "this user has not set it": capital then rejects with
    ``NO_CAPITAL`` exactly as it did before, and risk falls back to config.

    ``paused``/``pause_reason``/``paused_until`` mirror ``risk_state`` field for
    field, so one ``PauseState`` helper round-trips both. They hold this user's
    **daily-loss** pause and nothing else — the operator's ``/pause`` is still one
    global row, because a member who cannot pause themselves must not be able to
    strand the owner without a stop button either.

    No foreign keys, matching every other table here; ``signals.user_id`` and
    ``gate_decisions.user_id`` are plain ``BigInteger`` for the same reason.
    """

    __tablename__ = "users"

    #: Telegram's id, not ours. ``autoincrement=False`` because an integer primary
    #: key would otherwise be a BIGSERIAL, and an insert that forgot the id would
    #: quietly create user 1 instead of failing.
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Telegram ``full_name``. Usernames are optional on Telegram, and an approval
    #: request that reaches the owner as a bare integer is not a decision anyone
    #: can make.
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(String(8), nullable=False, default="MEMBER")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    capital_eur: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    risk_per_trade_pct: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Which wording was accepted. A disclaimer nobody can identify is not a record.
    acknowledged_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="", server_default=""
    )
    paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    pause_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Last time this id was told where it stands — the throttle that stops a
    #: rejected stranger from making the bot answer them repeatedly.
    notice_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_users_status", "status"),
        # There is exactly one owner, and the database says so rather than the
        # application remembering to. Two OWNER rows would mean two people can
        # admit users to somebody else's trading system, and the failure would be
        # silent — the second owner simply works. A partial unique index is the
        # cheapest place to make that impossible.
        Index(
            "uq_users_single_owner",
            "role",
            unique=True,
            postgresql_where=text("role = 'OWNER'"),
        ),
    )


# --------------------------------------------------------------------------- #
# M7 — the cycle, and the tracker's legs
# --------------------------------------------------------------------------- #


class CycleRow(Base):
    """One run of the scan cycle (ARCHITECTURE.md §3's cycle orchestrator).

    Two jobs. It makes ``/health``'s ``last_cycle_age_seconds`` survive a restart —
    an in-memory timestamp reports "never ran" after every deploy, which is the
    one moment the figure matters most. And it gives PRD G4 ("≥99% scan-cycle
    completion over 30 days; no missed cycles longer than 2 consecutive
    intervals") something countable: a reliability target with no row per attempt
    is an aspiration.

    A cycle that dies mid-flight leaves ``status='RUNNING'`` with no
    ``finished_at``. That is deliberate evidence, not a defect — the same posture
    as ``telegram_messages``' stuck PENDING claim.
    """

    __tablename__ = "cycles"

    cycle_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Which market this row belongs to (M10a). Backfilled to ``crypto`` by
    #: migration 0010, and NOT NULL with a server default so app code that predates
    #: the column can still insert against a migrated database.
    market: Mapped[str] = mapped_column(
        String(MARKET_COLUMN_LENGTH),
        nullable=False,
        server_default=text(f"'{LEGACY_MARKET.value}'"),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: RUNNING | OK | FAILED
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    symbols_requested: Mapped[int] = mapped_column(nullable=False, default=0)
    symbols_scanned: Mapped[int] = mapped_column(nullable=False, default=0)
    #: Ingestion failures plus the dedup/cooldown/cap skips, so a quiet cycle is
    #: explicable from the row rather than only from the logs.
    symbols_skipped: Mapped[int] = mapped_column(nullable=False, default=0)
    #: ``{symbol: {"reason": SkipReason, "detail": str}}`` — M8.2. The count above
    #: says a cycle was quiet; this says what it declined and why, in a fixed
    #: vocabulary M9 can GROUP BY. It is a column and not a log line because logs
    #: rotate and this is the record of what the system chose not to spend money on.
    skipped: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    candidates: Mapped[int] = mapped_column(nullable=False, default=0)
    analyzed: Mapped[int] = mapped_column(nullable=False, default=0)
    approved: Mapped[int] = mapped_column(nullable=False, default=0)
    published: Mapped[int] = mapped_column(nullable=False, default=0)

    #: What this cycle's LLM calls cost, by the same estimate the spend guard gates.
    spend_usd_estimate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("0")
    )
    #: True when the spend guard held the deep analyst back. The screener still ran.
    analysis_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspended_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    #: A cycle is per market from M10a: one scheduled job per enabled market, each
    #: writing its own rows. ``/pulse`` and the stale-cycle alert both ask "the last
    #: completed cycle *for this market*", which is this index.
    __table_args__ = (Index("ix_cycles_market_started_at", "market", "started_at"),)


class SignalFillRow(Base):
    """One entry rung that actually filled (RISK_ENGINE §3's "ladder metadata").

    §3 promises the card and the tracker that a rung-1-only stop-out is -0.40R.
    That promise is only keepable if the tracker knows *which* rungs filled, which
    is what this table is. Unique on ``(signal_id, rung_index)``: a rung fills once,
    and a tick re-run after a crash must not double it.
    """

    __tablename__ = "signal_fills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    #: 0-based index into ``TradePlan.entries``.
    rung_index: Mapped[int] = mapped_column(nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    qty: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: When the market reached it (the candle's close time), not when we noticed.
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: tracker | manual
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="tracker")

    __table_args__ = (
        UniqueConstraint("signal_id", "rung_index", name="uq_signal_fills_rung"),
        Index("ix_signal_fills_signal_id", "signal_id"),
    )


class SignalExitRow(Base):
    """One close: a target, the stop, an invalidation, an expiry, or a manual exit.

    Unique on ``(signal_id, kind)`` — TP1 happens once. A manual close is one event
    too: the owner reports the price they actually got, and a second report of the
    same close would double-count the realized R it produces.
    """

    __tablename__ = "signal_exits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    #: TP1 | TP2 | TP3 | STOP | INVALIDATION | EXPIRY | MANUAL
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    qty: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    exited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("signal_id", "kind", name="uq_signal_exits_kind"),
        Index("ix_signal_exits_signal_id", "signal_id"),
    )


class SignalEventRow(Base):
    """The tracker's append-only journal, and the only source of its notifications.

    Recording the event and posting it are separate steps on purpose, and the
    unique ``(signal_id, event_key)`` is what makes a mid-tick crash safe: the
    event is written once, and the notifier later posts every event that has no
    ``telegram_messages`` row yet. Neither half can duplicate the other's work,
    and a restart between them resolves forward rather than re-deciding.

    ``event_key`` is the same string ``telegram_messages.event_key`` carries.
    """

    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    #: Stable identity of *this* event for this signal: "fill:1", "tp:2", "stop".
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    realized_r: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    realized_eur: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: Everything the notification renders that is not a number — already-rendered
    #: fragments, never arithmetic for the card to perform (TELEGRAM_UX §1).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    detail: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("signal_id", "event_key", name="uq_signal_events_key"),
        Index("ix_signal_events_signal_id_at", "signal_id", "at"),
    )


class PersianSummaryRow(Base):
    """One Persian rewrite of one card (M11p). Its own table, never a column on
    ``signals``.

    **Why a separate table is the requirement and not a preference.** ``signals`` is
    read by ``/journal``, ``/stats`` and the three populations, and its shape is frozen
    for two live measurement windows. A column here would put a convenience feature
    inside the thing being measured. Delete every row in this table and nothing else in
    the system notices — which is the test of whether the separation is real.

    **``input_sha256`` is the key, and ``signal_id`` is not.** The obvious key would be
    the signal, and it is wrong twice over. One analysis produces one ``signals`` row
    *per approved user*, each sized against that user's own capital, so a signal id is
    already a per-user id; and a ``/pulse`` verdict card has no signal id at all. Keying
    on a hash of the exact text the model was shown gives the property the signal id was
    reached for -- identical input, identical output, one call -- for both card kinds,
    across users, without a special case. Two users whose plans genuinely differ get two
    rows, which is correct: they are reading two different cards.

    ``signal_id`` and ``analyst_report_id`` are kept as **nullable, indexed, and
    unconstrained** columns so the M13 dashboard can join in one hop. Deliberately no
    foreign key: an FK installs a constraint trigger on the *referenced* table, which
    would make deleting a signal depend on this table and put a dependency edge into
    ``signals`` -- the one thing this milestone may not touch.

    ``input_text`` is stored beside ``summary_text`` because the numbers rule is only
    auditable after the fact if both halves survive. It is also the evidence that the
    model was shown a card and nothing else.
    """

    __tablename__ = "persian_summaries"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    #: SHA-256, hex, of the exact text sent to the model. The cache key.
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``signal`` | ``pulse_verdict`` -- says which id column below is populated.
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    analyst_report_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    market: Mapped[str] = mapped_column(String(MARKET_COLUMN_LENGTH), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)

    #: The Persian words, exactly as sent. Without the reference line, which is
    #: appended at render time so that changing its wording does not require a
    #: migration and cannot leave old rows carrying a superseded sentence.
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: The card the model was shown, verbatim.
    input_text: Mapped[str] = mapped_column(Text, nullable=False)

    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)

    tokens_in: Mapped[int] = mapped_column(nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(nullable=False, default=0)
    #: Derived from token counts and config pricing, like every other cost here.
    cost_usd_estimate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("0")
    )
    #: The ``llm_calls`` row this came from. Nullable and unconstrained for the same
    #: reason the two ids above are.
    llm_call_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Who paid for it. The per-user daily generation cap counts these.
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("input_sha256", name="uq_persian_summaries_input_sha256"),
        Index("ix_persian_summaries_signal_id", "signal_id"),
        Index("ix_persian_summaries_analyst_report_id", "analyst_report_id"),
        Index(
            "ix_persian_summaries_user_created_at",
            "created_by_user_id",
            "created_at",
        ),
    )
