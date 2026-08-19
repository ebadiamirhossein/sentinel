"""Users, roles, owner approval, and the per-user signal

M8.1. One new table and two altered ones — the schema behind "one analysis per
cycle, shared; sizing, rails, decisions and stats per user".

* ``users`` — a Telegram id, where it stands (PENDING/APPROVED/REJECTED/
  SUSPENDED/LEFT), its role (OWNER/MEMBER), **its own capital and risk %**, its
  first-run acknowledgement, and its own daily-loss pause. Through M8 identity was
  ``TELEGRAM_ALLOWED_USER_IDS``: one env var serving as both the authorization
  list and the broadcast list, which only ever worked for one person. That env var
  survives to *name* the owner; this table is the runtime authority.

  ``uq_users_single_owner`` is a **partial** unique index on ``role`` where
  ``role = 'OWNER'``. Two owners would mean two people can admit users to someone
  else's trading system, and the failure would be silent — the second owner simply
  works. The database refuses instead.

* ``signals`` gains ``user_id``. This is the load-bearing decision of the
  milestone: one shared ``AnalystReport`` produces one row per approved user, each
  with its own sizing in ``plan``, its own ``decision``, its own fills and its own
  realized R. Every per-user query downstream — the open-risk rail, ``/positions``,
  ``/stats``' REAL population — is then a filter on a column rather than a join
  through a second table, and the tracker, the R accounting and the state machine
  are untouched. The alternative (one signal row, a ``(signal_id, user_id)``
  decision table) needs a per-user plan, filled quantity, status and realized R
  too, i.e. it grows into this table with extra steps.

* ``gate_decisions`` gains ``user_id``. The same report can approve for one user
  and reject with ``MAX_OPEN_RISK`` for another; without the column those two
  verdicts contradict each other in the audit trail (PRD G5).

**The owner id, and why this migration can fail.** Backfilling the two new NOT
NULL columns means naming an owner, and guessing one is worse than a failed
``alembic upgrade``: it would silently hand somebody else's signals, decisions and
measured history to whatever id happened to be first. So the owner is read from
``TELEGRAM_OWNER_USER_ID``, or from ``TELEGRAM_ALLOWED_USER_IDS`` when that holds
exactly one id, and if neither resolves **and there is data to attribute**, this
raises with the variable's name in the message. A completely fresh database needs
no owner here — ``sentinel/core/app.py`` seeds one at boot.

**What is preserved.** Existing signals and gate decisions become the owner's,
row for row; nothing is deleted or reassigned. The owner's ``runtime_settings``
values for ``capital_eur`` and ``risk_per_trade_pct`` are **copied** onto the
owner's row — and the ``runtime_settings`` rows are deliberately left in place.
Nothing reads capital or risk from there any more (only ``watchlist`` reaches
``config_overrides``), so there is no double-application risk, and the owner's
configuration history stays exactly where it was written.

No foreign keys, matching every earlier migration.

Revision ID: 0007_users_and_multi_user
Revises: 0006_tracker_and_stats
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_users_and_multi_user"
down_revision: str | None = "0006_tracker_and_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(38, 18)

#: The acknowledgement wording the owner is recorded as having accepted. Hardcoded
#: rather than imported from ``sentinel.bot.models.ACK_VERSION``: a migration is a
#: statement about one instant, and a future version bump must not retroactively
#: change what this one wrote.
OWNER_ACK_VERSION = "v1"

#: The two columns that carry the user dimension. Kept in one mapping so upgrade
#: and downgrade cannot drift apart.
USER_COLUMNS: tuple[str, ...] = ("signals", "gate_decisions")

_MISSING_OWNER = (
    "Cannot run 0007_users_and_multi_user: this database has signals, gate "
    "decisions or runtime settings to attribute, and no owner could be resolved. "
    "Set TELEGRAM_OWNER_USER_ID to your numeric Telegram user id (or leave exactly "
    "one id in TELEGRAM_ALLOWED_USER_IDS) and run the migration again. Guessing an "
    "owner would hand your signals, decisions and measured history to whichever id "
    "happened to be first."
)


def _resolve_owner_id() -> int | None:
    """The owner's Telegram id from the configured environment, or ``None``.

    ``TELEGRAM_ALLOWED_USER_IDS`` is only trusted when it holds a single id: a
    multi-id allowlist predates roles and says nothing about which of them owns
    the system.

    The values come through ``Secrets`` rather than ``os.environ`` because that is
    where they actually live — a deployment keeps them in ``.env``, which the
    process environment does not carry, and a migration that read only ``os.environ``
    would fail on every correctly configured machine. The *rule* stays frozen here
    (explicit first, sole allowlist entry second) so a later change to
    ``Secrets.owner_user_id`` cannot retroactively alter what this migration did.
    """
    from sentinel.core.config import Secrets

    secrets = Secrets()
    if secrets.telegram_owner_user_id is not None:
        return secrets.telegram_owner_user_id
    allowed = secrets.allowed_user_ids
    return allowed[0] if len(allowed) == 1 else None


def _has_data(connection: sa.Connection) -> bool:
    """Whether anything in this database would need an owner to attribute it."""
    for table in (*USER_COLUMNS, "runtime_settings"):
        count = connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if count:
            return True
    return False


def upgrade() -> None:
    connection = op.get_bind()
    owner_id = _resolve_owner_id()
    if owner_id is None and _has_data(connection):
        raise RuntimeError(_MISSING_OWNER)

    op.create_table(
        "users",
        # ``autoincrement=False`` is load-bearing. An integer primary key is a
        # BIGSERIAL by default, which would give this column a ``nextval`` — so an
        # insert that forgot the Telegram id would quietly create user 1, 2, 3
        # instead of failing. This is a natural key: it comes from Telegram, it is
        # never generated here, and a missing one must be an error.
        sa.Column("telegram_user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("role", sa.String(8), nullable=False, server_default="MEMBER"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("capital_eur", PRICE, nullable=True),
        sa.Column("risk_per_trade_pct", PRICE, nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_version", sa.String(16), nullable=False, server_default=""),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pause_reason", sa.String(32), nullable=True),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("telegram_user_id", name=op.f("pk_users")),
    )
    op.create_index("ix_users_status", "users", ["status"])
    op.create_index(
        "uq_users_single_owner",
        "users",
        ["role"],
        unique=True,
        postgresql_where=sa.text("role = 'OWNER'"),
    )

    # The user dimension. A NOT NULL column added to a populated table needs a
    # default to backfill from, and it is dropped immediately afterwards: from here
    # on every writer supplies the value explicitly, and a row that forgot to
    # should fail loudly rather than silently belong to the owner. The placeholder
    # for an empty database is never read by anything, because there are no rows.
    backfill = str(owner_id if owner_id is not None else 0)
    for table in USER_COLUMNS:
        op.add_column(
            table, sa.Column("user_id", sa.BigInteger(), nullable=False, server_default=backfill)
        )
        op.alter_column(table, "user_id", server_default=None)
    op.create_index("ix_signals_user_id_created_at", "signals", ["user_id", "created_at"])

    if owner_id is None:
        return

    # The owner is APPROVED and already acknowledged: they wrote the disclaimer,
    # and asking them to accept their own words before their own system will talk
    # to them is theatre that would silently stop every signal until they tapped it.
    connection.execute(
        sa.text(
            "INSERT INTO users (telegram_user_id, status, role, requested_at, "
            "decided_at, decided_by_user_id, acknowledged_at, acknowledged_version) "
            "VALUES (:owner, 'APPROVED', 'OWNER', now(), now(), :owner, now(), :version) "
            "ON CONFLICT (telegram_user_id) DO NOTHING"
        ),
        {"owner": owner_id, "version": OWNER_ACK_VERSION},
    )

    # ``runtime_settings.value`` is JSONB holding a JSON scalar — ``bot/runtime.py``
    # stores Decimals as strings. ``#>> '{}'`` reads the scalar as text whether it
    # was written as a string or a number, and a missing key yields NULL, which is
    # exactly "this owner never set it".
    for column, key in (
        ("capital_eur", "capital_eur"),
        ("risk_per_trade_pct", "risk_per_trade_pct"),
    ):
        connection.execute(
            sa.text(
                f"UPDATE users SET {column} = ("  # column is a literal from the loop above
                "  SELECT (value #>> '{}')::numeric FROM runtime_settings WHERE key = :key"
                ") WHERE telegram_user_id = :owner"
            ),
            {"key": key, "owner": owner_id},
        )


def downgrade() -> None:
    # Reversing this loses the users table: every approval, every acknowledgement
    # with its timestamp, and every member's capital and risk %. The signals stay,
    # and so do the owner's ``runtime_settings`` — this migration only ever copied
    # from them, never moved or deleted them, so a downgraded system reads the same
    # global capital it read before M8.1.
    op.drop_index("ix_signals_user_id_created_at", table_name="signals")
    for table in reversed(USER_COLUMNS):
        op.drop_column(table, "user_id")

    op.drop_index("uq_users_single_owner", table_name="users")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_table("users")
