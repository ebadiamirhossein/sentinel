"""baseline — empty starting point for the migration chain

No tables: M0 ships the scaffold only. The domain schema (snapshots, LLM I/O,
signals, fills, outcomes, config changes) arrives in later milestones, each as its
own migration — the DB schema is never touched outside one (CLAUDE.md).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
