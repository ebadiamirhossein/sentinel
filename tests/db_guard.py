"""Opt-in Postgres round-trips: the shared, and deliberately paranoid, entry point.

**These tests are destructive.** Every DB round-trip module in this suite asserts
absolute row counts, so each one truncates the tables it touches — at setup as
well as teardown, since M5.1 §5 (a fixture that only cleaned up afterwards failed
against any database that already held data).

That is fine against a scratch database and catastrophic against a live one. It
already cost something real: during M6's verification the suite was pointed at the
development database while it held a delivered signal, and it deleted the row the
card's buttons referred to. Pressing ✅ Taken then answered "That signal is no
longer in the database" — the handler being *correct* about a row the tests had
removed. At M9 the same mistake would delete measured trading history.

So the guard is here, not in a comment:

* ``SENTINEL_TEST_DATABASE_URL`` must be set — unchanged, the tests skip without it;
* and it must **differ** from ``DATABASE_URL``, the database the app itself uses.

Create the scratch database once:

    docker compose exec postgres createdb -U sentinel sentinel_test
    SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://sentinel:<pw>@localhost:5432/sentinel_test \\
        .venv/bin/alembic upgrade head
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from sentinel.core.config import Secrets

TEST_DB_URL = os.getenv("SENTINEL_TEST_DATABASE_URL")


def _same_database_as_the_app() -> bool:
    """True when the test URL points at the database the app itself uses.

    Compared after stripping credentials: ``sentinel:pw@localhost/sentinel`` and
    ``sentinel@localhost/sentinel`` are the same database, and a guard fooled by a
    password is not a guard.
    """
    if TEST_DB_URL is None:
        return False

    def target(url: str) -> str:
        host_and_path = url.rsplit("@", 1)[-1]
        return host_and_path.replace("127.0.0.1", "localhost").lower()

    return target(TEST_DB_URL) == target(Secrets().database_url)


SKIP_REASON = (
    "set SENTINEL_TEST_DATABASE_URL to run DB round-trip tests" if TEST_DB_URL is None else ""
)

REFUSE_REASON = (
    "SENTINEL_TEST_DATABASE_URL points at the same database as DATABASE_URL. "
    "These tests TRUNCATE the tables they assert on and would delete live signals. "
    "Use a scratch database: docker compose exec postgres createdb -U sentinel sentinel_test"
)


def requires_db(func: Any) -> Any:
    """Skip without a test database; fail loudly if it is the app's own."""
    func = pytest.mark.allow_socket(func)
    if TEST_DB_URL is not None and _same_database_as_the_app():
        return pytest.mark.skip(reason=REFUSE_REASON)(func)
    return pytest.mark.skipif(TEST_DB_URL is None, reason=SKIP_REASON)(func)


__all__ = ["REFUSE_REASON", "SKIP_REASON", "TEST_DB_URL", "requires_db"]
