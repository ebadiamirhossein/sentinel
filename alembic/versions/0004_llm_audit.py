"""LLM audit trail: every call, and every validated analyst report

M5. Two tables:

* ``llm_calls`` — one row per request/response, successful or not. Failures are
  rows too: PRD §6's "JSON validity >= 98%" is a ratio over this table, and M8's
  spend guard sums ``cost_usd_estimate`` from it. Images are stored as
  ``{sha256, params}`` references inside ``request``, not as bytes — a chart
  re-renders byte-identically from stored OHLCV (M3), so PRD F4 reconstruction
  holds without ~400KB of PNG per analyst call.
* ``analyst_reports`` — one row per schema-valid report, with the hot fields
  broken out of the JSONB so /stats can group without a JSON path. ``role``
  ships now, defaulting to ``'primary'``, because specs/ENSEMBLE.md §3 writes the
  GPT second opinion here as ``role='shadow'`` at M10; adding the column now is
  free, adding it later would rewrite live rows.

Revision ID: 0004_llm_audit
Revises: 0003_risk_state
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_llm_audit"
down_revision: str | None = "0003_risk_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("cycle_id", sa.UUID(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("refusal_category", sa.String(length=32), nullable=True),
        sa.Column("error", sa.String(length=1024), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd_estimate", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index("ix_llm_calls_started_at", "llm_calls", ["started_at"])
    op.create_index("ix_llm_calls_symbol_started_at", "llm_calls", ["symbol", "started_at"])
    op.create_index("ix_llm_calls_prompt_version_status", "llm_calls", ["prompt_version", "status"])
    op.create_index("ix_llm_calls_cycle_id", "llm_calls", ["cycle_id"])

    op.create_table(
        "analyst_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("cycle_id", sa.UUID(), nullable=True),
        sa.Column("snapshot_id", sa.UUID(), nullable=True),
        sa.Column("llm_call_id", sa.UUID(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="primary"),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("candidate_status", sa.String(length=16), nullable=False),
        sa.Column("setup_type", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("thesis", sa.String(length=1024), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyst_reports")),
    )
    op.create_index(
        "ix_analyst_reports_symbol_created_at", "analyst_reports", ["symbol", "created_at"]
    )
    op.create_index("ix_analyst_reports_prompt_version", "analyst_reports", ["prompt_version"])
    op.create_index("ix_analyst_reports_setup_type", "analyst_reports", ["setup_type"])
    op.create_index("ix_analyst_reports_cycle_id", "analyst_reports", ["cycle_id"])


def downgrade() -> None:
    op.drop_index("ix_analyst_reports_cycle_id", table_name="analyst_reports")
    op.drop_index("ix_analyst_reports_setup_type", table_name="analyst_reports")
    op.drop_index("ix_analyst_reports_prompt_version", table_name="analyst_reports")
    op.drop_index("ix_analyst_reports_symbol_created_at", table_name="analyst_reports")
    op.drop_table("analyst_reports")

    op.drop_index("ix_llm_calls_cycle_id", table_name="llm_calls")
    op.drop_index("ix_llm_calls_prompt_version_status", table_name="llm_calls")
    op.drop_index("ix_llm_calls_symbol_started_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_started_at", table_name="llm_calls")
    op.drop_table("llm_calls")
