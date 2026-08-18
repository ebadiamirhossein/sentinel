"""Signals, Telegram message ids, and the runtime-config layer

M6. Four tables, each closing a hole the earlier milestones left open.

* ``signals`` — ARCHITECTURE.md §3 contract 5, landing here because idempotent
  posting and a persisted button state both need a row to hang off. The full
  ``TradePlan`` is stored as JSONB rather than exploded into columns:
  specs/RISK_ENGINE.md §7 requires a signal's sizing to stay immutable after
  ``/capital`` changes, and the surest way to promise that is to keep exactly what
  the owner was shown. ``plan_id`` is unique — that constraint is what makes a
  restart unable to publish the same plan twice. ``number`` is an IDENTITY so a
  card can say "Signal #142" from a value that never moves; a ``count(*)`` would
  change if a row were ever removed. M7's tracker adds fill/outcome columns to
  this table rather than reshaping it, and ``status`` already speaks §3's state
  machine vocabulary.
* ``telegram_messages`` — specs/TELEGRAM_UX.md §6, "message ids stored; restarts
  never double-post". The unique ``(signal_id, kind, chat_id)`` *is* that promise:
  a row is claimed PENDING before the send and confirmed SENT after, so a crash in
  between leaves an auditable claim instead of a silent gap.
* ``runtime_settings`` — ARCHITECTURE.md §2 promises "DB > yaml > defaults", but
  until now the DB layer did not exist and ``load_config``'s ``db_overrides``
  parameter had no source. ``capital_eur`` lives nowhere else at all: config.yaml
  deliberately omits it and the gate rejects with ``NO_CAPITAL`` until ``/capital``
  writes it here.
* ``config_changes`` — PRD F10 requires Postgres to hold config *changes*, not
  merely current values. Kept separate so reading a current value stays a
  primary-key lookup.

No foreign keys, matching every earlier migration: cross-table ids are bare UUID
columns, so a table can be rebuilt or backfilled without a dependency dance.

Revision ID: 0005_signals_and_telegram
Revises: 0004_llm_audit
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_signals_and_telegram"
down_revision: str | None = "0004_llm_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("cycle_id", sa.UUID(), nullable=True),
        sa.Column("number", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("setup_type", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("chart_params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signals")),
        sa.UniqueConstraint("plan_id", name="uq_signals_plan_id"),
    )
    op.create_index("ix_signals_symbol_created_at", "signals", ["symbol", "created_at"])
    op.create_index("ix_signals_status", "signals", ["status"])
    op.create_index("ix_signals_decision", "signals", ["decision"])

    op.create_table(
        "telegram_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_messages")),
        sa.UniqueConstraint("signal_id", "kind", "chat_id", name="uq_telegram_messages_signal_id"),
    )
    op.create_index("ix_telegram_messages_status", "telegram_messages", ["status"])

    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_runtime_settings")),
    )

    op.create_table(
        "config_changes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_changes")),
    )
    op.create_index("ix_config_changes_key_changed_at", "config_changes", ["key", "changed_at"])


def downgrade() -> None:
    op.drop_index("ix_config_changes_key_changed_at", table_name="config_changes")
    op.drop_table("config_changes")

    op.drop_table("runtime_settings")

    op.drop_index("ix_telegram_messages_status", table_name="telegram_messages")
    op.drop_table("telegram_messages")

    op.drop_index("ix_signals_decision", table_name="signals")
    op.drop_index("ix_signals_status", table_name="signals")
    op.drop_index("ix_signals_symbol_created_at", table_name="signals")
    op.drop_table("signals")
