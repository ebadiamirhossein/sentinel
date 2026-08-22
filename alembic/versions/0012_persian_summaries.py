"""Persian card summaries

M11p. **One new table and nothing else** — no ALTER, no column on ``signals``, no
index on any existing table, no backfill. Two live measurement windows are open
(crypto reviewed ~2026-09-03, forex 2026-09-04) and ``alembic upgrade head`` runs at
container start, so this migration's whole design goal is that ``/journal``, ``/stats``
and the three populations cannot tell it ran.

**Why the key is a hash of the card and not the signal id.** The obvious key is wrong
twice over. One analysis produces one ``signals`` row *per approved user*, each sized
against that user's own capital, so a signal id is already a per-user id — and a
``/pulse`` verdict card has no signal id at all. ``input_sha256`` is the SHA-256 of the
exact text the model was shown, so identical input yields identical output and one
paid call, for both card kinds and across users, with no special case. Two users whose
plans genuinely differ get two rows, which is right: they are reading two cards.

**No foreign keys.** ``signal_id`` and ``analyst_report_id`` are indexed and
unconstrained so the M13 dashboard can join in one hop. A real FK would install a
constraint trigger on the *referenced* table, which would make deleting a signal
depend on this table and put a dependency edge into ``signals``. Every other table in
this schema omits FKs for its own reasons; this one omits them for that one.

**The downgrade is real and was exercised.** ``upgrade -> downgrade -> upgrade`` was
run against a Postgres carrying the full schema, with ``\\d signals`` captured on both
sides and diffed — see journal/M11p_REPORT.md. Dropping this table loses cached
Persian text and nothing else: every row is reproducible from the card it summarises.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_persian_summaries"
down_revision: str | None = "0011_forex_spine"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "persian_summaries",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("signal_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("analyst_report_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_usd_estimate", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("llm_call_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_persian_summaries"),
        sa.UniqueConstraint("input_sha256", name="uq_persian_summaries_input_sha256"),
    )
    op.create_index("ix_persian_summaries_signal_id", "persian_summaries", ["signal_id"])
    op.create_index(
        "ix_persian_summaries_analyst_report_id", "persian_summaries", ["analyst_report_id"]
    )
    # The per-user daily generation cap's query: "how many did this user pay for since
    # midnight UTC". Composite and in this order because the user is the equality
    # predicate and the timestamp is the range one.
    op.create_index(
        "ix_persian_summaries_user_created_at",
        "persian_summaries",
        ["created_by_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_persian_summaries_user_created_at", table_name="persian_summaries")
    op.drop_index("ix_persian_summaries_analyst_report_id", table_name="persian_summaries")
    op.drop_index("ix_persian_summaries_signal_id", table_name="persian_summaries")
    op.drop_table("persian_summaries")
