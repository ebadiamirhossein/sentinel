"""M6 round-trips against a real Postgres (opt-in).

The hermetic suite models the two unique constraints the idempotency guarantee
rests on. This file proves the *database* actually enforces them — a fake that
agrees with a wrong assumption is worse than no fake at all.

    SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://sentinel:...@localhost:5432/sentinel \\
        .venv/bin/pytest tests/bot/test_persistence.py
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.bot.models import (
    MessageKind,
    MessageStatus,
    SignalDecision,
    SignalRecord,
    UserRole,
    UserStatus,
    WatchlistRequestStatus,
)
from sentinel.risk.models import TradePlan
from sentinel.screener.models import DirectionHint
from sentinel.storage.models import (
    AnalystReportRow,
    ConfigChangeRow,
    CycleRow,
    GateDecisionRow,
    LLMCallRow,
    RuntimeSettingRow,
    SignalRow,
    TelegramMessageRow,
    UserRow,
    WatchlistRequestRow,
)
from sentinel.storage.repositories import (
    AnalystReportRepository,
    CycleRepository,
    GateDecisionRepository,
    LLMCallRepository,
    RuntimeSettingsRepository,
    SignalRepository,
    TelegramMessageRepository,
    UserRepository,
    WatchlistRequestRepository,
)
from tests.bot_double import OWNER_ID
from tests.db_guard import TEST_DB_URL, requires_db
from tests.risk_double import PLAN_NOW, approved_plan


async def _clean(session: AsyncSession) -> None:
    """Before *and* after: M5.1 §5 — these tests assert counts, and a dirty
    database is exactly what a developer running them is most likely to have."""
    for table in (
        TelegramMessageRow,
        SignalRow,
        ConfigChangeRow,
        RuntimeSettingRow,
        UserRow,
        WatchlistRequestRow,
        # M8.4 — what /pulse reads.
        LLMCallRow,
        AnalystReportRow,
        GateDecisionRow,
        CycleRow,
    ):
        await session.execute(delete(table))
    await session.commit()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert TEST_DB_URL is not None
    engine = create_async_engine(TEST_DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _clean(session)
        yield session
        await session.rollback()
        await _clean(session)
    await engine.dispose()


#: A fixed instant for the M8.3 request tests — nothing here reads a clock.
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def plan() -> TradePlan:
    return approved_plan()


@requires_db
async def test_a_signal_round_trips_with_its_whole_plan(
    session: AsyncSession, plan: TradePlan
) -> None:
    repo = SignalRepository(session)
    claimed = await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID))
    assert claimed is not None
    await session.commit()

    row = await repo.get_by_plan_id(plan.plan_id)
    assert row is not None
    assert row.symbol == "SOLUSDT"
    assert row.number >= 1, "the IDENTITY column must assign a human-facing number"
    # Decimals land as strings inside JSONB, as everywhere else in this codebase.
    assert row.plan["risk_eur"] == "74.98"
    assert row.plan["schema_version"] == 3
    assert row.plan["target_distances_pct"] == ["3.05", "4.75", "7.53"]
    assert row.plan["entries"][0]["distance_pct"] == "-0.36"


@requires_db
async def test_the_plan_id_constraint_is_what_stops_a_double_post(
    session: AsyncSession, plan: TradePlan
) -> None:
    """specs/TELEGRAM_UX.md §6, enforced by Postgres rather than by a code path."""
    repo = SignalRepository(session)
    assert await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID)) is not None
    await session.commit()

    # A *different* SignalRecord for the same plan — a retry, or a restart.
    assert await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID)) is None
    await session.commit()

    assert len(await repo.recent(user_id=OWNER_ID)) == 1


@requires_db
async def test_signal_numbers_are_stable_and_increasing(
    session: AsyncSession, plan: TradePlan
) -> None:
    repo = SignalRepository(session)
    first = await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID))
    second = await repo.claim(
        SignalRecord(plan=plan.model_copy(update={"plan_id": uuid4()}), user_id=OWNER_ID)
    )
    await session.commit()
    assert first is not None and second is not None
    assert second.number > first.number


@requires_db
async def test_a_message_can_be_claimed_once_and_confirmed(
    session: AsyncSession, plan: TradePlan
) -> None:
    record = await SignalRepository(session).claim(SignalRecord(plan=plan, user_id=OWNER_ID))
    assert record is not None
    await session.commit()

    repo = TelegramMessageRepository(session)
    assert await repo.claim(record.signal_id, MessageKind.CARD, 42, at=PLAN_NOW) is True
    await session.commit()

    # The second claim is the restart case: it must fail, not raise.
    assert await repo.claim(record.signal_id, MessageKind.CARD, 42, at=PLAN_NOW) is False
    await session.commit()

    await repo.confirm(record.signal_id, MessageKind.CARD, 42, message_id=777, at=PLAN_NOW)
    await session.commit()

    posted = await repo.get(record.signal_id, MessageKind.CARD, 42)
    assert posted is not None
    assert posted.message_id == 777
    assert posted.status is MessageStatus.SENT
    assert await repo.stuck() == []


@requires_db
async def test_the_album_and_the_card_are_separate_claims(
    session: AsyncSession, plan: TradePlan
) -> None:
    """Same signal, same chat, different kind — both must be claimable."""
    record = await SignalRepository(session).claim(SignalRecord(plan=plan, user_id=OWNER_ID))
    assert record is not None
    await session.commit()

    repo = TelegramMessageRepository(session)
    assert await repo.claim(record.signal_id, MessageKind.CHARTS, 42, at=PLAN_NOW) is True
    assert await repo.claim(record.signal_id, MessageKind.CARD, 42, at=PLAN_NOW) is True
    await session.commit()


@requires_db
async def test_an_unconfirmed_claim_shows_up_as_stuck(
    session: AsyncSession, plan: TradePlan
) -> None:
    """The crash window is visible in /status rather than silent."""
    record = await SignalRepository(session).claim(SignalRecord(plan=plan, user_id=OWNER_ID))
    assert record is not None
    repo = TelegramMessageRepository(session)
    await repo.claim(record.signal_id, MessageKind.CARD, 42, at=PLAN_NOW)
    await session.commit()

    stuck = await repo.stuck()
    assert [row.status for row in stuck] == [MessageStatus.PENDING.value]


@requires_db
async def test_a_decision_is_persisted_and_idempotent(
    session: AsyncSession, plan: TradePlan
) -> None:
    repo = SignalRepository(session)
    record = await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID))
    assert record is not None
    await session.commit()

    first = await repo.record_decision(
        record.signal_id, SignalDecision.TAKEN, at=PLAN_NOW, user_id=111
    )
    await session.commit()
    assert first is not None and first[1] is True

    again = await repo.record_decision(
        record.signal_id, SignalDecision.TAKEN, at=PLAN_NOW, user_id=111
    )
    assert again is not None and again[1] is False

    taken = await repo.with_decision(SignalDecision.TAKEN, user_id=OWNER_ID)
    assert [row.signal_id if hasattr(row, "signal_id") else row.id for row in taken] == [
        record.signal_id
    ]
    assert await repo.undecided_count(user_id=OWNER_ID) == 0


@requires_db
async def test_runtime_settings_upsert_and_leave_an_audit_trail(session: AsyncSession) -> None:
    """PRD F10 — config *changes*, not merely current values."""
    repo = RuntimeSettingsRepository(session)
    await repo.set("capital_eur", "10000", at=PLAN_NOW, user_id=111)
    await session.commit()
    await repo.set("capital_eur", "12000", at=datetime(2026, 8, 18, 13, 0, tzinfo=UTC), user_id=111)
    await session.commit()

    assert await repo.get("capital_eur") == "12000"
    assert await repo.all() == {"capital_eur": "12000"}

    changes = await repo.changes()
    assert [(row.old_value, row.new_value) for row in changes] == [
        ("10000", "12000"),
        (None, "10000"),
    ]


@requires_db
async def test_a_watchlist_survives_the_round_trip_as_a_list(session: AsyncSession) -> None:
    repo = RuntimeSettingsRepository(session)
    await repo.set("watchlist", ["BTCUSDT", "SOLUSDT"], at=PLAN_NOW, user_id=111)
    await session.commit()
    assert await repo.get("watchlist") == ["BTCUSDT", "SOLUSDT"]


@requires_db
async def test_no_float_reaches_the_stored_plan(session: AsyncSession, plan: TradePlan) -> None:
    """CLAUDE.md — money math is Decimal, and JSONB is where floats sneak in."""
    record = await SignalRepository(session).claim(SignalRecord(plan=plan, user_id=OWNER_ID))
    assert record is not None
    await session.commit()
    row = await SignalRepository(session).get_by_plan_id(plan.plan_id)
    assert row is not None

    def walk(payload: object) -> None:
        if isinstance(payload, dict):
            for value in payload.values():
                walk(value)
        elif isinstance(payload, list):
            for value in payload:
                walk(value)
        else:
            assert not isinstance(payload, float), f"float in the stored plan: {payload!r}"

    walk(row.plan)
    assert Decimal(row.plan["notional_eur"]) == plan.notional_eur


# --------------------------------------------------------------------------- #
# users — M8.1's table, against the real constraints
# --------------------------------------------------------------------------- #


@requires_db
async def test_a_second_start_from_the_same_id_writes_nothing(session: AsyncSession) -> None:
    """The anti-spam guarantee, against the real primary key rather than a fake one.

    ``request`` is ``ON CONFLICT DO NOTHING``: a stranger tapping /start twenty times
    produces one row and one owner notification, and a restart cannot lose the count
    because there is no count.
    """
    repo = UserRepository(session)
    first = await repo.request(4242, username="newcomer", display_name="New", at=PLAN_NOW)
    await session.commit()
    later = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
    second = await repo.request(4242, username="renamed", display_name="New", at=later)
    await session.commit()

    assert first is not None and first.status is UserStatus.PENDING
    assert second is None, "a re-request is a no-op, not an update"
    stored = await repo.get(4242)
    assert stored is not None
    assert stored.username == "newcomer", "the original row is untouched"
    assert stored.requested_at == PLAN_NOW


@requires_db
async def test_the_database_refuses_a_second_owner(session: AsyncSession) -> None:
    """``uq_users_single_owner`` — two owners would mean two people can admit users
    to somebody else's trading system, and the failure would be silent."""
    from sqlalchemy.exc import IntegrityError

    repo = UserRepository(session)
    await repo.ensure_owner(111, at=PLAN_NOW, acknowledged_version="v1")
    await session.commit()

    session.add(
        UserRow(
            telegram_user_id=222,
            status=UserStatus.APPROVED.value,
            role=UserRole.OWNER.value,
            requested_at=PLAN_NOW,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@requires_db
async def test_seeding_an_owner_twice_never_overwrites_the_first(
    session: AsyncSession,
) -> None:
    """``ensure_owner`` runs on every boot. If it were an upsert it would re-approve
    an owner who suspended themselves and reset a capital they had changed."""
    repo = UserRepository(session)
    await repo.ensure_owner(111, at=PLAN_NOW, acknowledged_version="v1")
    await repo.set_capital(111, Decimal("8000"), at=PLAN_NOW)
    await session.commit()

    again = await repo.ensure_owner(
        111, at=datetime(2026, 9, 1, tzinfo=UTC), acknowledged_version="v1"
    )
    await session.commit()

    assert again.capital_eur == Decimal("8000")
    assert again.requested_at == PLAN_NOW


@requires_db
async def test_the_sizing_inputs_leave_an_audit_trail(session: AsyncSession) -> None:
    """PRD F10 followed the value when it moved out of ``runtime_settings``.

    "Why was this signal sized against €8,000" must still have a timestamped answer,
    and the key is namespaced per user because ``config_changes`` is shared and a
    bare ``capital_eur`` would now be ambiguous.
    """
    repo = UserRepository(session)
    await repo.ensure_owner(111, at=PLAN_NOW, acknowledged_version="v1")
    await repo.set_capital(111, Decimal("8000"), at=PLAN_NOW)
    await session.commit()
    await repo.set_capital(111, Decimal("9000"), at=datetime(2026, 8, 18, 13, 0, tzinfo=UTC))
    await session.commit()

    changes = await RuntimeSettingsRepository(session).changes()
    assert [(row.key, row.new_value) for row in changes] == [
        ("user.111.capital_eur", "9000"),
        ("user.111.capital_eur", "8000"),
    ]
    # The previous value is read back from a Numeric(38,18) column, so it carries the
    # column's scale rather than the string that was typed. That is the honest
    # record: it is what the database held, not what a renderer would have shown.
    assert changes[0].old_value == "8000.000000000000000000"
    assert changes[1].old_value is None, "the first write has nothing to supersede"


@requires_db
async def test_a_users_pause_round_trips_as_a_pause_state(session: AsyncSession) -> None:
    """The per-user daily-loss pause reuses ``PauseState`` field for field, so one
    helper serves both it and ``risk_state``."""
    from sentinel.risk.models import PauseReason, PauseState

    repo = UserRepository(session)
    await repo.ensure_owner(111, at=PLAN_NOW, acknowledged_version="v1")
    until = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    await repo.set_pause(
        111,
        PauseState(paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=until),
        at=PLAN_NOW,
    )
    await session.commit()

    stored = await repo.get(111)
    assert stored is not None
    assert stored.pause.paused is True
    assert stored.pause.reason is PauseReason.DAILY_LOSS_LIMIT
    assert stored.pause.until == until


@requires_db
async def test_two_users_signals_never_appear_in_each_others_books(
    session: AsyncSession, plan: TradePlan
) -> None:
    """The privacy guarantee, asserted against real SQL rather than a fake filter."""
    repo = SignalRepository(session)
    mine = await repo.claim(SignalRecord(plan=plan, user_id=111))
    theirs = await repo.claim(
        SignalRecord(plan=plan.model_copy(update={"plan_id": uuid4()}), user_id=222)
    )
    await session.commit()
    assert mine is not None and theirs is not None

    assert [row.id for row in await repo.recent(user_id=111)] == [mine.signal_id]
    assert [row.id for row in await repo.recent(user_id=222)] == [theirs.signal_id]
    assert await repo.undecided_count(user_id=111) == 1
    assert await repo.open_symbols(user_id=111) == {plan.symbol}


@requires_db
async def test_only_one_request_per_symbol_can_be_pending(session: AsyncSession) -> None:
    """``uq_watchlist_requests_one_pending`` — the M8.3 guarantee, in Postgres (M8.3).

    ``tests/bot_double.py`` models this with a dict keyed by symbol, which is a
    *description* of the constraint rather than the constraint. If the partial index
    were missing from the migration, every hermetic test would still pass and two
    members asking for the same symbol in the same second would produce two rows and
    two cards. Only a real database can say the index is there.
    """
    repo = WatchlistRequestRepository(session)
    symbol = f"TEST{uuid4().hex[:6].upper()}"

    first = await repo.request(symbol, user_id=OWNER_ID, at=NOW)
    second = await repo.request(symbol, user_id=OWNER_ID + 1, at=NOW)

    assert first is not None
    assert second is None, "the second insert must be refused by the index, not by a check"
    pending = await repo.pending_for(symbol)
    assert pending is not None
    assert pending.requested_by_user_id == OWNER_ID


@requires_db
async def test_the_index_is_partial_so_a_decided_symbol_can_be_asked_for_again(
    session: AsyncSession,
) -> None:
    """The index covers PENDING rows only, which is what lets history be kept.

    A full unique index on ``symbol`` would forbid a second request for ever, and
    deleting decided rows to work around it would throw away the answer to "has
    anyone asked for this before".
    """
    repo = WatchlistRequestRepository(session)
    symbol = f"TEST{uuid4().hex[:6].upper()}"

    await repo.request(symbol, user_id=OWNER_ID, at=NOW)
    await repo.decide(symbol, WatchlistRequestStatus.REJECTED, by=OWNER_ID, at=NOW)
    again = await repo.request(symbol, user_id=OWNER_ID, at=NOW)

    assert again is not None, "a decided symbol must be requestable again"
    rows = (
        (
            await session.execute(
                select(WatchlistRequestRow).where(WatchlistRequestRow.symbol == symbol)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2, "the rejected row is kept, not overwritten"


# --------------------------------------------------------------------------- #
# M8.4 — the three reads behind /pulse
#
# journal/M8_3_REPORT.md §4's lesson, applied before it costs anything this time:
# ``FakeLLMCallRepository.screener_verdicts`` returns verdicts already made, so the
# JSONB path, the ``IS NOT NULL`` ordering and the ``IN`` filters below have never
# run in the hermetic suite. A typo in any of them is a ``ProgrammingError`` on the
# first ``/pulse`` in production — the one path every approved user is invited to
# take on the day it ships.
# --------------------------------------------------------------------------- #


PULSE_NOW = datetime(2026, 8, 20, 11, 0, tzinfo=UTC)


def _screener_call(cycle_id: object, *, status: str = "OK", **response: object) -> LLMCallRow:
    return LLMCallRow(
        cycle_id=cycle_id,
        symbol=None,
        kind="SCREENER",
        provider="anthropic",
        model="claude-sonnet-4-6",
        prompt_version="screener_v2",
        status=status,
        started_at=PULSE_NOW,
        request={},
        response=response,
    )


@requires_db
async def test_the_screener_verdicts_come_back_out_of_the_audit_row(
    session: AsyncSession,
) -> None:
    """The read that has no table of its own.

    The screener's answers live only inside ``llm_calls.response``, so this asserts
    the JSONB path *and* that the stored shape still validates against the current
    ``ScreenerVerdict`` — the two ways this read can rot independently of each other.
    """
    cycle_id = uuid4()
    session.add(
        _screener_call(
            cycle_id,
            parsed={
                "verdicts": [
                    {
                        "symbol": "SOLUSDT",
                        "interesting": True,
                        "direction_hint": "long",
                        "reason": "reclaimed the breakout",
                    },
                    {
                        "symbol": "ADAUSDT",
                        "interesting": False,
                        "direction_hint": "unclear",
                        "reason": "ranging",
                    },
                ]
            },
        )
    )
    await session.commit()

    found = await LLMCallRepository(session).screener_verdicts([cycle_id])
    verdicts = found[cycle_id]
    assert [v.symbol for v in verdicts] == ["SOLUSDT", "ADAUSDT"]
    assert verdicts[0].interesting is True
    assert verdicts[0].direction_hint is DirectionHint.LONG
    assert verdicts[1].interesting is False


@requires_db
async def test_a_discarded_screener_batch_contributes_no_verdicts(
    session: AsyncSession,
) -> None:
    """``INVALID_JSON`` rows are kept for M9 and are not answers about a market.

    Counting one would put a symbol on the pulse that ``screener.reconcile`` had
    already defaulted to not-interesting — the card would say the pipeline escalated
    something it never did, and would name it.
    """
    cycle_id = uuid4()
    session.add(
        _screener_call(
            cycle_id,
            status="INVALID_JSON",
            parsed={
                "verdicts": [
                    {
                        "symbol": "SOLUSDT",
                        "interesting": True,
                        "direction_hint": "long",
                        "reason": "…",
                    }
                ]
            },
        )
    )
    await session.commit()

    assert await LLMCallRepository(session).screener_verdicts([cycle_id]) == {}


@requires_db
async def test_an_ok_call_with_an_unreadable_body_yields_a_cycle_with_no_verdicts(
    session: AsyncSession,
) -> None:
    """The other half, and the reason ``/pulse`` needs its own "silent" state.

    ``_response_audit`` only fills ``parsed`` when the text was JSON; a model that
    answered in prose leaves ``text`` instead. The cycle is present with no verdicts,
    which is what ``PulseView.screener_silent`` renders as a *degraded* cycle rather
    than as a quiet market.
    """
    cycle_id = uuid4()
    session.add(_screener_call(cycle_id, text="I cannot answer that."))
    await session.commit()

    found = await LLMCallRepository(session).screener_verdicts([cycle_id])
    assert found == {cycle_id: ()}


@requires_db
async def test_chunked_screener_calls_are_merged_into_one_cycles_verdicts(
    session: AsyncSession,
) -> None:
    """``screener_batch_size`` splits the watchlist, so one cycle can hold several OK
    calls, each answering for its own symbols."""
    cycle_id = uuid4()
    for symbol in ("SOLUSDT", "LINKUSDT"):
        session.add(
            _screener_call(
                cycle_id,
                parsed={
                    "verdicts": [
                        {
                            "symbol": symbol,
                            "interesting": True,
                            "direction_hint": "long",
                            "reason": "r",
                        }
                    ]
                },
            )
        )
    await session.commit()

    found = await LLMCallRepository(session).screener_verdicts([cycle_id])
    assert {v.symbol for v in found[cycle_id]} == {"SOLUSDT", "LINKUSDT"}


@requires_db
async def test_latest_completed_ignores_a_cycle_still_running(session: AsyncSession) -> None:
    """The distinction ``/pulse`` exists on.

    A ``RUNNING`` row has escalated nothing and analysed nothing *yet*, which renders
    identically to a cycle that found nothing — and one of those is a working system.
    """
    finished = uuid4()
    session.add(
        CycleRow(
            cycle_id=finished,
            started_at=PULSE_NOW - timedelta(hours=1),
            finished_at=PULSE_NOW - timedelta(minutes=55),
            status="OK",
        )
    )
    session.add(CycleRow(cycle_id=uuid4(), started_at=PULSE_NOW, status="RUNNING"))
    await session.commit()

    latest = await CycleRepository(session).latest_completed()
    assert latest is not None and latest.cycle_id == finished


@requires_db
async def test_latest_completed_still_returns_a_failed_cycle(session: AsyncSession) -> None:
    """It finished, it has a partial story, and hiding it would leave the newest
    thing anybody could see silently stale."""
    failed = uuid4()
    session.add(
        CycleRow(
            cycle_id=failed,
            started_at=PULSE_NOW,
            finished_at=PULSE_NOW,
            status="FAILED",
            error="Binance 451",
        )
    )
    await session.commit()

    latest = await CycleRepository(session).latest_completed()
    assert latest is not None and latest.status == "FAILED"


@requires_db
async def test_completed_since_is_a_window_over_finished_cycles(session: AsyncSession) -> None:
    inside, outside = uuid4(), uuid4()
    session.add(
        CycleRow(
            cycle_id=inside,
            started_at=PULSE_NOW - timedelta(hours=2),
            finished_at=PULSE_NOW - timedelta(hours=2),
            status="OK",
        )
    )
    session.add(
        CycleRow(
            cycle_id=outside,
            started_at=PULSE_NOW - timedelta(hours=30),
            finished_at=PULSE_NOW - timedelta(hours=30),
            status="OK",
        )
    )
    session.add(CycleRow(cycle_id=uuid4(), started_at=PULSE_NOW, status="RUNNING"))
    await session.commit()

    found = await CycleRepository(session).completed_since(PULSE_NOW - timedelta(hours=24))
    assert [row.cycle_id for row in found] == [inside]


@requires_db
async def test_the_skipped_column_round_trips_as_a_map_pulse_can_read(
    session: AsyncSession,
) -> None:
    """M8.2's JSONB, read back the way ``/pulse`` reads it rather than the way the
    orchestrator writes it."""
    cycle_id = uuid4()
    session.add(
        CycleRow(
            cycle_id=cycle_id,
            started_at=PULSE_NOW,
            finished_at=PULSE_NOW,
            status="OK",
            skipped={"LINKUSDT": {"reason": "OPEN_SIGNAL", "detail": "already open"}},
        )
    )
    await session.commit()

    latest = await CycleRepository(session).latest_completed()
    assert latest is not None
    assert latest.skipped == {"LINKUSDT": {"reason": "OPEN_SIGNAL", "detail": "already open"}}


@requires_db
async def test_reports_and_decisions_are_fetched_per_cycle(session: AsyncSession) -> None:
    """Both ``IN`` filters at once, plus the ``role='primary'`` one.

    The shadow row is here because specs/ENSEMBLE.md §3 will start writing them at
    M10, and a pulse that listed both would read as the pipeline having analysed
    every symbol twice.
    """
    mine, other = uuid4(), uuid4()
    for cycle_id, role in ((mine, "primary"), (mine, "shadow"), (other, "primary")):
        session.add(
            AnalystReportRow(
                cycle_id=cycle_id,
                symbol="SOLUSDT",
                created_at=PULSE_NOW,
                role=role,
                provider="anthropic",
                model="claude-fable-5",
                prompt_version="fable_v1",
                candidate_status="CANDIDATE",
                setup_type="trend_pullback",
                direction="long",
                confidence=78,
                thesis="t",
                report={},
            )
        )
    for cycle_id, user_id in ((mine, 111), (mine, 222), (other, 111)):
        session.add(
            GateDecisionRow(
                cycle_id=cycle_id,
                user_id=user_id,
                symbol="SOLUSDT",
                evaluated_at=PULSE_NOW,
                gate_status="REJECTED",
                reason="RR_TOO_LOW",
                message="",
            )
        )
    await session.commit()

    reports = await AnalystReportRepository(session).for_cycles([mine])
    assert [row.role for row in reports] == ["primary"]

    decisions = await GateDecisionRepository(session).for_cycles([mine])
    assert sorted(row.user_id for row in decisions) == [111, 222], (
        "every user's verdict, not just one — /pulse folds them itself"
    )


@requires_db
async def test_the_pulse_reads_are_all_no_ops_on_an_empty_id_list(
    session: AsyncSession,
) -> None:
    """A cycle that escalated nothing produces no ids, and ``IN ()`` is not valid SQL
    everywhere it looks like it should be. Guarded in each query; asserted here."""
    assert await LLMCallRepository(session).screener_verdicts([]) == {}
    assert await AnalystReportRepository(session).for_cycles([]) == []
    assert await GateDecisionRepository(session).for_cycles([]) == []
