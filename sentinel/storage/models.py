"""ORM models — the first real schema (M1).

Shape chosen with the owner: candles are normalized and upserted so a 15-minute
cycle does not rewrite 700 rows per symbol, while the per-cycle context that is
genuinely snapshot-shaped (funding, book, news, sentiment, FX, degradation) is
stored as JSONB on the snapshot row. A signal stays fully reconstructable from
the DB alone (PRD F10).

Money-ish columns are ``Numeric`` and map to ``Decimal`` — never float.
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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.storage.base import Base

#: Wide enough for any crypto price or notional without losing precision.
PRICE = Numeric(38, 18)


class MarketSnapshotRow(Base):
    """One symbol, one cycle: the analyst's full input, minus the candle series."""

    __tablename__ = "market_snapshots"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    cycle_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
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
    tick_size: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    qty_step: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    min_notional: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    contract_size: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("1"))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    """

    __tablename__ = "gate_decisions"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    cycle_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
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


class IngestionFailureRow(Base):
    """Why a symbol was skipped or a source degraded — auditability (PRD G5)."""

    __tablename__ = "ingestion_failures"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
        Index("ix_llm_calls_started_at", "started_at"),
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

    #: Human-facing sequence, so a card can say "Signal #142" without exposing a
    #: UUID. A Postgres IDENTITY rather than a count(*): the number must be stable
    #: once printed on a card, and a count changes if a row is ever deleted.
    number: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
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

    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_signals_plan_id"),
        Index("ix_signals_symbol_created_at", "symbol", "created_at"),
        Index("ix_signals_status", "status"),
        Index("ix_signals_decision", "decision"),
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
    #: charts | card | update — one of each per signal per chat.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Null while the claim is PENDING; set when Telegram confirms the send.
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("signal_id", "kind", "chat_id", name="uq_telegram_messages_signal_id"),
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
