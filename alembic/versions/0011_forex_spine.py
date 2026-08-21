"""The forex data spine

M10b-1. Three changes, one of which touches an existing table and two of which are
new and empty.

**1. ``ohlcv_candles.volume`` becomes nullable.**

specs/FOREX.md §2.1 is categorical: where an input does not exist the feature is
absent — not zero, not a placeholder. Saxo publishes no volume field of any kind for
FxSpot, not even tick counts. A zero standing in for "no data" reads to a model as
"no activity" and yields a confident answer built on nothing, which is the failure
this project keeps meeting from other directions.

Until now there was nowhere for that absence to land: ``Candle.volume`` was a
required Pydantic field and this column was NOT NULL. Both change together (spec
defect #13, recorded 2026-08-21 — §2.1 stated the rule and never noticed the
contract made it impossible).

**This is a catalogue-only change. It backfills nothing and rewrites no row.**
``DROP NOT NULL`` updates ``pg_attribute`` and does not touch the heap, which
matters on the largest table in the schema. No existing crypto volume becomes null,
because nothing here writes to the column at all — verified by
``ops/verify-migration.sh``, which asserts row counts are unchanged and that no
crypto row acquired a null volume.

The obvious objection is the one migration 0008 records against a nullable
``cycles.skipped``: a nullable column makes "no value" and "predates the column"
indistinguishable. It does not apply here, because the distinction is carried
elsewhere and strictly — ``sentinel.ingestion.models.assert_volume_matches_market``
refuses a crypto candle with no volume **and** a forex candle with one, in both
directions, at construction and again at the storage seam. The schema is permissive
so that forex can be honest; the code is not.

**The downgrade cannot simply restore NOT NULL**, because by then there may be forex
rows that legitimately have no volume. It deletes forex candles first — they are
re-fetchable from the venue in one request per timeframe, they belong to a market
that is disabled in the shipped config, and the alternative is a downgrade that
fails at 3am on the one table nobody wants to be holding a lock on.

**2. ``forex_instruments``** — resolved Uics and their cross-checked pips. A table of
its own rather than rows in ``instrument_meta``: that model is a tick size, a
quantity step, a minimum *notional* and a contract size, and forex has a Uic, a
``Format.Decimals``, a pip and a minimum *trade size* in base units. Three of those
have no column there and the fourth would put a units figure where a money one is
read. specs/FOREX.md §4.2 says "cached in ``instrument_meta``"; that is corrected
here and recorded in the corrections log.

**3. ``saxo_oauth_tokens``** — one row, id fixed at 1 by a check constraint. The
refresh token is single-use and rotates, so a second row is not a stale answer but a
spent one. Both expiry columns are nullable: a bootstrap token pasted in after a
manual browser login has no lifetime we were told, and inventing one would be the
same "assume an operational value" mistake spike defect D-c is about.

Both new tables are empty by construction, so up → down → up is clean.

Verified against a restored copy of a populated production dump with
``ops/verify-migration.sh``, up and down — never against the live database, which
that script hard-refuses.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_forex_spine"
down_revision: str | None = "0010_market_dimension"
branch_labels: str | None = None
depends_on: str | None = None

PRICE = sa.Numeric(38, 18)
FOREX = "forex"


def upgrade() -> None:
    # ---- 1. volume becomes optional, for the one market that has none --------
    op.alter_column("ohlcv_candles", "volume", existing_type=PRICE, nullable=True)

    # ---- 2. resolved instruments, with the pip that was actually used --------
    op.create_table(
        "forex_instruments",
        sa.Column("symbol", sa.String(length=32), primary_key=True),
        sa.Column("uic", sa.BigInteger(), nullable=False),
        sa.Column("decimals", sa.BigInteger(), nullable=False),
        sa.Column("pip", PRICE, nullable=False),
        sa.Column("tick_size", PRICE, nullable=False),
        sa.Column("min_trade_size", PRICE, nullable=False),
        sa.Column("amount_decimals", sa.BigInteger(), nullable=False),
        sa.Column("base_currency", sa.String(length=8), nullable=False),
        sa.Column("quote_currency", sa.String(length=8), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ---- 3. the rotating OAuth credential, exactly one row -------------------
    op.create_table(
        "saxo_oauth_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("access_token", sa.String(length=2048), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token", sa.String(length=2048), nullable=False),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("obtained_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("id = 1", name="ck_saxo_oauth_tokens_single_row"),
    )


def downgrade() -> None:
    op.drop_table("saxo_oauth_tokens")
    op.drop_table("forex_instruments")

    # Forex candles are the only rows that can hold a null volume, and NOT NULL
    # cannot be restored while they exist. They are re-fetchable in one request per
    # timeframe from a venue that keeps ~50 days of hourly history, and they belong
    # to a market the shipped config disables — so deleting them costs a re-fetch,
    # whereas leaving them would make this downgrade fail.
    op.execute(sa.text(f"DELETE FROM ohlcv_candles WHERE market = '{FOREX}'"))
    op.alter_column("ohlcv_candles", "volume", existing_type=PRICE, nullable=False)
