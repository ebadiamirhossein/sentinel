"""Member watchlist requests

M8.3. One new table, ``watchlist_requests``: a member asks for a symbol, the owner
approves or rejects.

The watchlist decides what the shared deep analyst is pointed at, and an analyst
call is ~$0.28 on the owner's key — so members cannot edit it directly, and this
table is the asking. It is the same shape as M8.1's registration flow one level
down: a request row, a card with two buttons, and a decision that is recorded
rather than inferred.

**``uq_watchlist_requests_one_pending`` is the load-bearing part.** A partial unique
index over ``symbol WHERE status = 'PENDING'`` makes "one pending request per
symbol" a property of the database rather than a check in a handler. Two members
asking for LINKUSDT in the same second produce one row and one card; the second
insert is an ``ON CONFLICT DO NOTHING`` that creates nothing and notifies nobody.
A pre-check plus an insert would be a race, and a counter would be state to lose in
a restart — the same reasoning that made M8.1's "one request per id" a primary key.

**Partial, so decided rows can stay.** A rejected symbol may be asked for again
later: the reason to hold a symbol off the watchlist is a fact about the market, and
markets change. A full unique index on ``symbol`` would forbid that forever, and
deleting decided rows instead would make "has anyone asked for this before"
unanswerable — the same objection as every other place in this project where
measured history is kept rather than cleaned up.

No foreign keys to ``users``, matching every other table here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_watchlist_requests"
down_revision: str | None = "0008_cycle_skips"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "watchlist_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("requested_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_watchlist_requests"),
    )
    op.create_index(
        "uq_watchlist_requests_one_pending",
        "watchlist_requests",
        ["symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index("ix_watchlist_requests_status", "watchlist_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_watchlist_requests_status", table_name="watchlist_requests")
    op.drop_index("uq_watchlist_requests_one_pending", table_name="watchlist_requests")
    op.drop_table("watchlist_requests")
