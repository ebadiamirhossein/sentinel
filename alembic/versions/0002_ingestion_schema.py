"""ingestion schema — snapshots, candles, instrument meta, FX, failures

First real schema (M1). Shape: candles normalized and upserted so a 15-minute
cycle does not rewrite the whole series; the per-cycle context that is genuinely
snapshot-shaped lives in JSONB on the snapshot row.

Revision ID: 0002_ingestion_schema
Revises: 0001_baseline
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_ingestion_schema"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(precision=38, scale=18)


def upgrade() -> None:
    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("cycle_id", sa.UUID(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("last_price", PRICE, nullable=False),
        sa.Column("data_quality", sa.String(length=16), nullable=False),
        sa.Column("degraded_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_snapshots")),
    )
    op.create_index(
        "ix_market_snapshots_symbol_captured_at",
        "market_snapshots",
        ["symbol", "captured_at"],
        unique=False,
    )
    op.create_index("ix_market_snapshots_cycle_id", "market_snapshots", ["cycle_id"], unique=False)

    op.create_table(
        "ohlcv_candles",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("volume", PRICE, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "timeframe", "open_time", name=op.f("pk_ohlcv_candles")),
    )

    op.create_table(
        "instrument_meta",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("tick_size", PRICE, nullable=False),
        sa.Column("qty_step", PRICE, nullable=False),
        sa.Column("min_notional", PRICE, nullable=False),
        sa.Column("contract_size", PRICE, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("symbol", name=op.f("pk_instrument_meta")),
    )

    op.create_table(
        "fx_rates",
        sa.Column("pair", sa.String(length=16), nullable=False),
        sa.Column("rate", PRICE, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("pair", name=op.f("pk_fx_rates")),
    )

    op.create_table(
        "ingestion_failures",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cycle_id", sa.UUID(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_failures")),
        sa.UniqueConstraint("cycle_id", "symbol", "source", name="uq_ingestion_failures_cycle"),
    )


def downgrade() -> None:
    op.drop_table("ingestion_failures")
    op.drop_table("fx_rates")
    op.drop_table("instrument_meta")
    op.drop_table("ohlcv_candles")
    op.drop_index("ix_market_snapshots_cycle_id", table_name="market_snapshots")
    op.drop_index("ix_market_snapshots_symbol_captured_at", table_name="market_snapshots")
    op.drop_table("market_snapshots")
