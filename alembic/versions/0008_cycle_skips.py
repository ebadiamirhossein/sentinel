"""Why a cycle declined to analyse a symbol, as data

M8.2. One column: ``cycles.skipped``, a JSONB map of
``{symbol: {"reason": SkipReason, "detail": str}}``.

Through M8.1 the reason a screened symbol never reached the deep analyst existed
only as a ``cycle.symbol_skipped`` log line. That was enough while the question was
"why was this cycle quiet", which someone asks once, at the time, with the logs
still in front of them. It is not enough for the question M9 has to answer — *what
is the system declining to analyse, and why* — because that one is asked over weeks,
in aggregate, after the logs have rotated. Container logs here are capped at 10 MB
x 5 per service (journal/M8_REPORT.md §6), so the window is days, not months.

**Why a fixed vocabulary.** The reasons were free text ("on cooldown until ...",
"daily signal cap reached (5)"), which cannot be grouped: every cooldown skip is a
distinct string. ``SkipReason`` closes the set — OPEN_SIGNAL, COOLDOWN, DAILY_CAP,
NO_FUNDED_USER, PAUSED, SPEND_LIMIT, RECENTLY_ANALYSED — and the prose survives
beside it as ``detail``, because a cooldown is genuinely more useful with its expiry
attached. So::

    SELECT e.value->>'reason' AS reason, count(*)
    FROM cycles c, jsonb_each(c.skipped) e
    GROUP BY 1 ORDER BY 2 DESC;

**Why a column on ``cycles`` and not a table.** A skip has no life of its own: it is
scoped to exactly one cycle, is written once when that cycle closes, and is never
updated or referenced by anything else. A ``cycle_skips`` table would add a join and
a second write for a value that is always read with its parent row.

NOT NULL with a ``'{}'`` server default, so the eight cycles already on the server
read as "skipped nothing recorded" rather than NULL — the same reasoning as M7's
``telegram_messages.event_key``: a nullable column would make "no skips" and "this
predates the column" indistinguishable, and both would then have to be handled
everywhere downstream.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_cycle_skips"
down_revision: str | None = "0007_users_and_multi_user"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "cycles",
        sa.Column(
            "skipped",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("cycles", "skipped")
