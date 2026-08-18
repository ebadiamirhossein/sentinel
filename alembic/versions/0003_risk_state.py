"""risk-gate decisions and pause state

M4. Two tables:

* ``gate_decisions`` — every verdict the risk gate reaches, approved or not, with
  a machine-readable reason code (PRD F10/G5; M9 groups rejections by it).
* ``risk_state`` — one row holding the pause state, because specs/RISK_ENGINE.md
  §7 requires a pause to survive a restart.

Revision ID: 0003_risk_state
Revises: 0002_ingestion_schema
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_risk_state"
down_revision: str | None = "0002_ingestion_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gate_decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("cycle_id", sa.UUID(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gate_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("message", sa.String(length=512), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=True),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gate_decisions")),
    )
    op.create_index(
        "ix_gate_decisions_symbol_evaluated_at", "gate_decisions", ["symbol", "evaluated_at"]
    )
    op.create_index("ix_gate_decisions_reason", "gate_decisions", ["reason"])

    op.create_table(
        "risk_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("pause_reason", sa.String(length=32), nullable=True),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_state")),
    )


def downgrade() -> None:
    op.drop_table("risk_state")
    op.drop_index("ix_gate_decisions_reason", table_name="gate_decisions")
    op.drop_index("ix_gate_decisions_symbol_evaluated_at", table_name="gate_decisions")
    op.drop_table("gate_decisions")
