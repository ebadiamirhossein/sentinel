"""The market dimension

M10a. Sentinel has always been a crypto system, and every row in the database is a
crypto row — implicitly, by there being nothing else. A second market arrives in
M10b, and a system that learns to hold two of something *after* it has a year of
data is a system that spends that milestone backfilling instead of building. So the
dimension lands first, while there is one market to backfill and one meaning it can
have.

**Every existing row becomes ``crypto``.** That is not a guess: no forex code
exists, no forex adapter is registered, and the config ships with forex disabled.

**Why NOT NULL with a server default rather than a nullable column.**
``ADD COLUMN ... NOT NULL DEFAULT`` is a catalogue-only operation on PostgreSQL 11+,
so this does not rewrite ``ohlcv_candles`` — the one table here with a serious row
count. A nullable column would have made "no market recorded" and "this row predates
the column" indistinguishable for ever, which is the exact objection migration 0008
records against a nullable ``cycles.skipped``.

**The default is kept, not dropped afterwards.** If the owner rolls the app back to
the previous commit against a migrated database — which ``ops/update.sh``'s rollback
path tells them to do — the old code inserts rows with no ``market`` at all. With the
default in place those inserts succeed and land as ``crypto``, which is what they
are. Without it they would fail, and the rollback would not be a rollback.

**Which tables, and which not.** Rows that belong to exactly one market get the
column. ``signal_fills``/``signal_exits``/``signal_events``/``telegram_messages``
do not: each hangs off a ``signal_id`` that already carries it, and a copy is a
second place for the answer to disagree. ``fx_rates`` does not: EUR→USD is a display
rate every market reads. ``users``, ``runtime_settings`` and ``config_changes`` are
about people and settings. ``risk_state`` does not, because it stays the *global*
operator pause and "all markets" is not a market — the per-market pause is a new
table, so the row the owner's ``/pause`` writes in an emergency is untouched.

``ohlcv_candles`` gains the column but keeps its ``(symbol, timeframe, open_time)``
primary key. Symbols are disjoint across markets today (``BTCUSDT`` vs ``EURUSD``),
so the key is still correct, and widening a primary key means dropping and rebuilding
it on the biggest table in the schema for no query that exists. Flagged in
journal/M10a_REPORT.md for M10b rather than done speculatively here.

**Three indexes are replaced rather than added.** ``ix_signals_user_id_created_at``,
``ix_cycles_started_at`` and ``ix_llm_calls_started_at`` all become market-leading.
Every reader of those three now knows its market at the call site, so a second
overlapping index would cost every insert to serve a query nobody issues.

**``uq_watchlist_requests_one_pending`` becomes ``(market, symbol)``.** A duplicate
pending request is an ``ON CONFLICT DO NOTHING`` that notifies nobody, so a
cross-market collision here would be silent — the worst kind this project has.

**The stored watchlist is renamed.** ``runtime_settings['watchlist']`` becomes
``watchlist:crypto``. The row is the live watchlist on the server, so it is moved
rather than left for the application to guess at.

Verified against a restored copy of a populated production dump with
``ops/verify-migration.sh``, up and down — never against the live database, which
that script hard-refuses.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_market_dimension"
down_revision: str | None = "0009_watchlist_requests"
branch_labels: str | None = None
depends_on: str | None = None

#: Every table that gains ``market``. The value backfilled into all of them.
MARKET_TABLES: tuple[str, ...] = (
    "market_snapshots",
    "ohlcv_candles",
    "instrument_meta",
    "ingestion_failures",
    "llm_calls",
    "analyst_reports",
    "gate_decisions",
    "signals",
    "watchlist_requests",
    "cycles",
)

LEGACY_MARKET = "crypto"
MARKET_LENGTH = 16


def _market_column() -> sa.Column[str]:
    return sa.Column(
        "market",
        sa.String(length=MARKET_LENGTH),
        nullable=False,
        server_default=sa.text(f"'{LEGACY_MARKET}'"),
    )


def upgrade() -> None:
    for table in MARKET_TABLES:
        op.add_column(table, _market_column())

    # ---- indexes: market-leading, replacing rather than duplicating ----------
    op.drop_index("ix_signals_user_id_created_at", table_name="signals")
    op.create_index(
        "ix_signals_market_user_id_created_at",
        "signals",
        ["market", "user_id", "created_at"],
    )

    op.drop_index("ix_cycles_started_at", table_name="cycles")
    op.create_index("ix_cycles_market_started_at", "cycles", ["market", "started_at"])

    op.drop_index("ix_llm_calls_started_at", table_name="llm_calls")
    op.create_index("ix_llm_calls_market_started_at", "llm_calls", ["market", "started_at"])

    op.drop_index("uq_watchlist_requests_one_pending", table_name="watchlist_requests")
    op.create_index(
        "uq_watchlist_requests_one_pending",
        "watchlist_requests",
        ["market", "symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # ---- the two pause tables (M10a Step 5) ---------------------------------
    op.create_table(
        "market_pause_state",
        sa.Column("market", sa.String(length=MARKET_LENGTH), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("pause_reason", sa.String(length=32), nullable=True),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("market", name="pk_market_pause_state"),
    )
    op.create_table(
        "user_market_pauses",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("market", sa.String(length=MARKET_LENGTH), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("pause_reason", sa.String(length=32), nullable=True),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", "market", name="pk_user_market_pauses"),
    )
    op.create_index("ix_user_market_pauses_user_id", "user_market_pauses", ["user_id"])

    # ---- the stored watchlist becomes per market ----------------------------
    # An UPDATE and not a delete-and-insert: ``updated_at`` and
    # ``updated_by_user_id`` record who last edited the watchlist, and that history
    # is as true of the renamed key as it was of the old one.
    op.execute(
        sa.text("UPDATE runtime_settings SET key = 'watchlist:crypto' WHERE key = 'watchlist'")
    )
    op.execute(
        sa.text("UPDATE config_changes SET key = 'watchlist:crypto' WHERE key = 'watchlist'")
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE config_changes SET key = 'watchlist' WHERE key = 'watchlist:crypto'")
    )
    op.execute(
        sa.text("UPDATE runtime_settings SET key = 'watchlist' WHERE key = 'watchlist:crypto'")
    )

    op.drop_index("ix_user_market_pauses_user_id", table_name="user_market_pauses")
    op.drop_table("user_market_pauses")
    op.drop_table("market_pause_state")

    op.drop_index("uq_watchlist_requests_one_pending", table_name="watchlist_requests")
    op.create_index(
        "uq_watchlist_requests_one_pending",
        "watchlist_requests",
        ["symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.drop_index("ix_llm_calls_market_started_at", table_name="llm_calls")
    op.create_index("ix_llm_calls_started_at", "llm_calls", ["started_at"])

    op.drop_index("ix_cycles_market_started_at", table_name="cycles")
    op.create_index("ix_cycles_started_at", "cycles", ["started_at"])

    op.drop_index("ix_signals_market_user_id_created_at", table_name="signals")
    op.create_index("ix_signals_user_id_created_at", "signals", ["user_id", "created_at"])

    # Reverse order, so a table is never left holding a column its indexes assume.
    for table in reversed(MARKET_TABLES):
        op.drop_column(table, "market")
