"""The outcome tracker, the cycle log, and the widened message key

M7. Four new tables and two altered ones — the schema behind "every signal's
outcome is tracked automatically" (PRD G2).

* ``cycles`` — one row per 15-minute scan. It makes ``/health``'s
  ``last_cycle_age_seconds`` survive a restart (an in-memory timestamp reports
  "never ran" after every deploy, which is exactly when the figure matters most)
  and gives PRD G4's "≥99% scan-cycle completion over 30 days" something
  countable. A cycle that dies mid-flight leaves ``status='RUNNING'`` with no
  ``finished_at``: deliberate evidence, the same posture as a stuck PENDING claim.
* ``signal_fills`` / ``signal_exits`` — the legs that actually happened.
  specs/RISK_ENGINE.md §3 promises the card that a rung-1-only stop-out is
  -0.40R, and that promise is only keepable if the tracker records *which* rungs
  filled. Unique on ``(signal_id, rung_index)`` and ``(signal_id, kind)``: a rung
  fills once and TP1 happens once, so a tick re-run after a crash cannot double
  either.
* ``signal_events`` — the append-only tracker journal, and the only source of the
  §4 notification replies. Recording an event and posting it are separate steps,
  and the unique ``(signal_id, event_key)`` is what makes the gap between them
  safe: the event is written once, and the notifier posts every event that has no
  ``telegram_messages`` row yet.
* ``signals`` gains the tracker's half — fill roll-ups, realized R and EUR,
  realized costs (M5.1 §10 asked for these rather than the plan's estimate),
  outcome, and the timestamps a restart resumes from. Plus ``dry_run``, so a
  signal produced by a rehearsal cycle is tracked and measured but never
  published and never mixed into the real or hypothetical statistics.
* ``telegram_messages`` gains ``event_key`` and the unique key widens to
  ``(signal_id, kind, chat_id, event_key)``. A signal's thread carries many
  updates and ``kind`` alone allowed exactly one.

  **``event_key`` is NOT NULL with a ``''`` default, and that is the point.**
  Postgres treats two NULLs as distinct inside a unique index, so a nullable
  column would have silently un-guaranteed the *card's* idempotency — the promise
  specs/TELEGRAM_UX.md §6 exists to make. Existing rows backfill to ``''`` and
  keep exactly the guarantee they had.

No foreign keys, matching every earlier migration.

Revision ID: 0006_tracker_and_stats
Revises: 0005_signals_and_telegram
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "0006_tracker_and_stats"
down_revision: str | None = "0005_signals_and_telegram"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(38, 18)

#: The tracker columns added to ``signals``, in one list so upgrade and downgrade
#: cannot drift apart.
SIGNAL_COLUMNS: tuple[sa.Column[object], ...] = (
    sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("filled_qty", PRICE, nullable=False, server_default="0"),
    sa.Column("avg_fill_price", PRICE, nullable=True),
    sa.Column("stop_price_current", PRICE, nullable=True),
    sa.Column("tp_hits", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("realized_r", PRICE, nullable=True),
    sa.Column("realized_eur", PRICE, nullable=True),
    sa.Column("realized_costs_eur", PRICE, nullable=True),
    sa.Column("outcome", sa.String(24), nullable=True),
    sa.Column("first_fill_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    op.create_table(
        "cycles",
        sa.Column("cycle_id", PgUUID(as_uuid=True), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="RUNNING"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("symbols_requested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("symbols_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("symbols_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("analyzed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("approved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spend_usd_estimate", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("analysis_suspended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("suspended_reason", sa.String(64), nullable=True),
        sa.Column("error", sa.String(512), nullable=True),
    )
    op.create_index("ix_cycles_started_at", "cycles", ["started_at"])

    op.create_table(
        "signal_fills",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("signal_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("rung_index", sa.Integer(), nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("qty", PRICE, nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="tracker"),
        sa.UniqueConstraint("signal_id", "rung_index", name="uq_signal_fills_rung"),
    )
    op.create_index("ix_signal_fills_signal_id", "signal_fills", ["signal_id"])

    op.create_table(
        "signal_exits",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("signal_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("qty", PRICE, nullable=False),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("signal_id", "kind", name="uq_signal_exits_kind"),
    )
    op.create_index("ix_signal_exits_signal_id", "signal_exits", ["signal_id"])

    op.create_table(
        "signal_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("signal_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=True),
        sa.Column("price", PRICE, nullable=True),
        sa.Column("realized_r", PRICE, nullable=True),
        sa.Column("realized_eur", PRICE, nullable=True),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("detail", sa.String(512), nullable=False, server_default=""),
        sa.UniqueConstraint("signal_id", "event_key", name="uq_signal_events_key"),
    )
    op.create_index("ix_signal_events_signal_id_at", "signal_events", ["signal_id", "at"])

    for column in SIGNAL_COLUMNS:
        op.add_column("signals", column)
    op.create_index("ix_signals_status_expires_at", "signals", ["status", "expires_at"])
    op.create_index("ix_signals_closed_at", "signals", ["closed_at"])

    # The widened message key. Add the column with a default first so existing
    # rows are backfilled to '' *before* the new constraint is created — a NULL
    # here would make the unique index stop guaranteeing anything.
    op.add_column(
        "telegram_messages",
        sa.Column("event_key", sa.String(64), nullable=False, server_default=""),
    )
    op.drop_constraint("uq_telegram_messages_signal_id", "telegram_messages", type_="unique")
    op.create_unique_constraint(
        "uq_telegram_messages_signal_id",
        "telegram_messages",
        ["signal_id", "kind", "chat_id", "event_key"],
    )


def downgrade() -> None:
    # Reversing the message key drops rows' distinguishing column, so any signal
    # that received more than one update would violate the narrower constraint.
    # Delete the update rows first: they are notification receipts, and the events
    # themselves live in ``signal_events``, which this same downgrade removes.
    op.execute("DELETE FROM telegram_messages WHERE event_key <> ''")
    op.drop_constraint("uq_telegram_messages_signal_id", "telegram_messages", type_="unique")
    op.create_unique_constraint(
        "uq_telegram_messages_signal_id",
        "telegram_messages",
        ["signal_id", "kind", "chat_id"],
    )
    op.drop_column("telegram_messages", "event_key")

    op.drop_index("ix_signals_closed_at", table_name="signals")
    op.drop_index("ix_signals_status_expires_at", table_name="signals")
    for column in reversed(SIGNAL_COLUMNS):
        op.drop_column("signals", column.name)

    op.drop_index("ix_signal_events_signal_id_at", table_name="signal_events")
    op.drop_table("signal_events")
    op.drop_index("ix_signal_exits_signal_id", table_name="signal_exits")
    op.drop_table("signal_exits")
    op.drop_index("ix_signal_fills_signal_id", table_name="signal_fills")
    op.drop_table("signal_fills")
    op.drop_index("ix_cycles_started_at", table_name="cycles")
    op.drop_table("cycles")
