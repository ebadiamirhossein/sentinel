"""Audit persistence (PRD F10/G5).

Serialization is pure and always runs; the round-trips need a real Postgres and
are opt-in via ``SENTINEL_TEST_DATABASE_URL``, matching M1 and M4.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.analyst.models import AnalystReport, CandidateStatus, Direction, SetupType
from sentinel.llm.models import LLMCall, LLMCallKind, LLMCallStatus, TokenUsage
from sentinel.storage.models import AnalystReportRow, LLMCallRow
from sentinel.storage.repositories import (
    AnalystReportRepository,
    LLMCallRepository,
    analyst_report_row,
    llm_call_row,
)
from tests.bot_double import OWNER_ID
from tests.db_guard import TEST_DB_URL, requires_db

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def a_call(**overrides: object) -> LLMCall:
    base: dict[str, object] = {
        "kind": LLMCallKind.ANALYST,
        "provider": "anthropic",
        "model": "claude-fable-5",
        "prompt_version": "fable_v1",
        "symbol": "SOLUSDT",
        "status": LLMCallStatus.OK,
        "usage": TokenUsage(input_tokens=12000, output_tokens=1500),
        "cost_usd_estimate": Decimal("0.195000"),
        "duration_ms": 8421,
        "started_at": NOW,
        "request": {"system": "S", "text_blocks": ["hello"], "images": []},
        "response": {"parsed": {"candidate_status": "CANDIDATE"}},
    }
    base.update(overrides)
    return LLMCall(**base)


def a_report() -> AnalystReport:
    return AnalystReport(
        symbol="SOLUSDT",
        candidate_status=CandidateStatus.CANDIDATE,
        setup_type=SetupType.TREND_PULLBACK,
        direction=Direction.LONG,
        thesis="4h up, 1h pullback.",
        confidence=78,
        prompt_version="fable_v1",
        model="claude-fable-5",
    )


# ── pure serialization ─────────────────────────────────────────────────────


def test_call_row_flattens_usage_and_keeps_money_decimal() -> None:
    row = llm_call_row(a_call())
    assert row["tokens_in"] == 12000
    assert row["tokens_out"] == 1500
    assert row["cost_usd_estimate"] == Decimal("0.195000")
    assert isinstance(row["cost_usd_estimate"], Decimal)
    assert row["status"] == "OK"


def test_failed_call_serializes_too() -> None:
    """A refusal is a row. PRD §6's JSON-validity ratio is computed over these."""
    row = llm_call_row(
        a_call(status=LLMCallStatus.REFUSAL, refusal_category="cyber", error="declined")
    )
    assert row["status"] == "REFUSAL"
    assert row["refusal_category"] == "cyber"


def test_long_error_is_truncated_to_the_column() -> None:
    row = llm_call_row(a_call(error="x" * 5000))
    assert len(row["error"]) == 1024


def test_screener_call_has_no_symbol() -> None:
    row = llm_call_row(a_call(kind=LLMCallKind.SCREENER, symbol=None))
    assert row["symbol"] is None
    assert row["kind"] == "SCREENER"


def test_report_row_breaks_out_the_groupable_fields() -> None:
    """`/stats` groups by these; a JSON path would be slower and untyped."""
    row = analyst_report_row(a_report(), created_at=NOW, provider="fable5")
    assert row["candidate_status"] == "CANDIDATE"
    assert row["setup_type"] == "trend_pullback"
    assert row["direction"] == "long"
    assert row["prompt_version"] == "fable_v1"
    assert row["report"]["symbol"] == "SOLUSDT"


def test_report_row_defaults_to_primary_role() -> None:
    """specs/ENSEMBLE.md §3 writes role='shadow' here at M10 -- an insert, not a migration."""
    assert analyst_report_row(a_report(), created_at=NOW, provider="fable5")["role"] == "primary"
    shadow = analyst_report_row(a_report(), created_at=NOW, provider="gpt56sol", role="shadow")
    assert shadow["role"] == "shadow"


def test_request_audit_holds_image_references_not_bytes() -> None:
    call = a_call(
        request={"system": "S", "text_blocks": [], "images": [{"sha256": "ab" * 32, "params": {}}]}
    )
    row = llm_call_row(call)
    assert row["request"]["images"][0]["sha256"] == "ab" * 32


# ── round-trip against a real database ─────────────────────────────────────


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine(TEST_DB_URL or "", future=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()
    await engine.dispose()


@requires_db
@pytest.mark.allow_socket
@pytest.mark.asyncio
async def test_llm_call_round_trip(session: AsyncSession) -> None:
    call = a_call(cycle_id=uuid4())
    await LLMCallRepository(session).record(call)
    await session.flush()

    stored = await session.get(LLMCallRow, call.call_id)
    assert stored is not None
    assert stored.model == "claude-fable-5"
    assert stored.cost_usd_estimate == Decimal("0.195000")
    assert stored.request["text_blocks"] == ["hello"]


@requires_db
@pytest.mark.allow_socket
@pytest.mark.asyncio
async def test_recent_for_symbol_returns_newest_first(session: AsyncSession) -> None:
    """The query behind specs/PROMPTS.md §3's history block."""
    repo = AnalystReportRepository(session)
    symbol = f"TEST{uuid4().hex[:6].upper()}"
    for index in range(4):
        report = a_report().model_copy(update={"symbol": symbol, "confidence": 70 + index})
        await repo.save(
            report,
            created_at=NOW.replace(minute=index * 10),
            provider="fable5",
        )
    await session.flush()

    verdicts = await repo.recent_for_symbol(symbol, limit=3, owner_id=OWNER_ID)

    assert len(verdicts) == 3
    assert [v.confidence for v in verdicts] == [73, 72, 71]
    # These reports carry no ``cycle_id``, so there is no signal or gate decision
    # to match them to and the block renders "outcome not resolved yet". The
    # populated path is exercised in tests/analyst/test_history.py and against
    # real rows in tests/tracker/test_persistence.py.
    assert all(v.outcome is None for v in verdicts), "an unmatched report has no outcome"


@requires_db
@pytest.mark.allow_socket
@pytest.mark.asyncio
async def test_shadow_reports_do_not_pollute_primary_history(session: AsyncSession) -> None:
    """Forward-check for M10: the history block must stay Fable's own record."""
    repo = AnalystReportRepository(session)
    symbol = f"TEST{uuid4().hex[:6].upper()}"
    await repo.save(
        a_report().model_copy(update={"symbol": symbol}), created_at=NOW, provider="fable5"
    )
    await repo.save(
        a_report().model_copy(update={"symbol": symbol}),
        created_at=NOW,
        provider="gpt56sol",
        role="shadow",
    )
    await session.flush()

    assert len(await repo.recent_for_symbol(symbol, owner_id=OWNER_ID)) == 1
    total = await session.execute(
        select(func.count()).select_from(AnalystReportRow).where(AnalystReportRow.symbol == symbol)
    )
    assert total.scalar_one() == 2


@requires_db
@pytest.mark.allow_socket
@pytest.mark.asyncio
async def test_latest_non_candidates_takes_the_newest_verdict_per_symbol(
    session: AsyncSession,
) -> None:
    """The M8.2 re-analysis cooldown's query, against real SQL.

    The join is the whole subtlety and no fake exercises it: a symbol analysed twice
    in the window must be judged on the **later** verdict, so a WATCHLIST followed by
    a CANDIDATE is *not* suppressed. Getting that backwards would silently mute a
    symbol at the exact moment it became interesting — the most expensive possible
    failure for a rail whose entire purpose is saving money.
    """
    repo = AnalystReportRepository(session)
    stale = f"TEST{uuid4().hex[:6].upper()}"
    turned = f"TEST{uuid4().hex[:6].upper()}"
    never = f"TEST{uuid4().hex[:6].upper()}"

    base = a_report()
    # Still quiet: two WATCHLIST looks, the later one at :30.
    for minute in (10, 30):
        await repo.save(
            base.model_copy(
                update={"symbol": stale, "candidate_status": CandidateStatus.WATCHLIST}
            ),
            created_at=NOW.replace(minute=minute),
            provider="fable5",
        )
    # Turned interesting: WATCHLIST at :10, CANDIDATE at :30 — must NOT be suppressed.
    await repo.save(
        base.model_copy(update={"symbol": turned, "candidate_status": CandidateStatus.WATCHLIST}),
        created_at=NOW.replace(minute=10),
        provider="fable5",
    )
    await repo.save(
        base.model_copy(update={"symbol": turned, "candidate_status": CandidateStatus.CANDIDATE}),
        created_at=NOW.replace(minute=30),
        provider="fable5",
    )
    # Outside the window entirely.
    await repo.save(
        base.model_copy(update={"symbol": never, "candidate_status": CandidateStatus.WATCHLIST}),
        created_at=NOW.replace(hour=NOW.hour - 5, minute=0),
        provider="fable5",
    )
    await session.flush()

    quiet = await repo.latest_non_candidates(since=NOW.replace(minute=0))

    assert quiet.get(stale) == NOW.replace(minute=30), "the newer of two verdicts wins"
    assert turned not in quiet, "a symbol whose latest verdict is CANDIDATE is not suppressed"
    assert never not in quiet, "outside the window"
