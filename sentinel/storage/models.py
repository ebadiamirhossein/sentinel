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
