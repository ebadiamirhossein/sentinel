"""Repositories — the only place that turns domain models into rows.

Serialization is split out into pure functions (`snapshot_context`,
`snapshot_sources`, `candle_rows`) so it can be tested without a database.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.analyst.history import PastVerdict
from sentinel.analyst.models import AnalystReport, CandidateStatus, SetupType
from sentinel.bot.models import (
    OPEN_STATUSES,
    MessageKind,
    MessageStatus,
    PostedMessage,
    SignalDecision,
    SignalRecord,
    SignalStatus,
    UserAccount,
    UserRole,
    UserStatus,
)
from sentinel.ingestion.models import FxRate, InstrumentMeta, MarketSnapshot, Stamped
from sentinel.llm.models import LLMCall
from sentinel.llm.spend import SpendTotals
from sentinel.risk.models import GateDecision, GateStatus, PauseReason, PauseState, TradePlan
from sentinel.storage.models import (
    AnalystReportRow,
    ConfigChangeRow,
    CycleRow,
    FxRateRow,
    GateDecisionRow,
    IngestionFailureRow,
    InstrumentMetaRow,
    LLMCallRow,
    MarketSnapshotRow,
    OhlcvCandleRow,
    RiskStateRow,
    RuntimeSettingRow,
    SignalEventRow,
    SignalExitRow,
    SignalFillRow,
    SignalRow,
    TelegramMessageRow,
    UserRow,
)

#: Snapshot parts stored as JSONB on the snapshot row.
CONTEXT_FIELDS = (
    "instrument",
    "derivatives",
    "orderbook",
    "news",
    "sentiment",
    "macro",
    "fx",
    "features",
)


def snapshot_context(snapshot: MarketSnapshot) -> dict[str, Any]:
    """JSON-safe context payload (candles excluded — they have their own table)."""
    dumped = snapshot.model_dump(mode="json", include=set(CONTEXT_FIELDS))
    return {key: dumped.get(key) for key in CONTEXT_FIELDS}


def snapshot_sources(snapshot: MarketSnapshot) -> dict[str, Any]:
    """``{field: {source, fetched_at}}`` for every stamped part (§3)."""
    sources: dict[str, Any] = {}

    for timeframe, series in snapshot.ohlcv.items():
        sources[f"ohlcv_{timeframe}"] = {
            "source": series.source,
            "fetched_at": series.fetched_at.isoformat(),
            "candles": len(series.candles),
        }

    for field in CONTEXT_FIELDS:
        part = getattr(snapshot, field, None)
        if not isinstance(part, Stamped):  # `features` and absent parts carry no stamp
            continue
        sources[field] = {
            "source": part.source,
            "fetched_at": part.fetched_at.isoformat(),
        }

    return sources


def candle_rows(snapshot: MarketSnapshot) -> list[dict[str, Any]]:
    """Flatten every timeframe's candles into upsertable row dicts."""
    rows: list[dict[str, Any]] = []
    for timeframe, series in snapshot.ohlcv.items():
        rows.extend(
            {
                "symbol": snapshot.symbol,
                "timeframe": timeframe,
                "open_time": candle.open_time,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "source": series.source,
                "fetched_at": series.fetched_at,
            }
            for candle in series.candles
        )
    return rows


class SnapshotRepository:
    """Persists snapshots: metadata + context row, candles upserted separately."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, snapshot: MarketSnapshot) -> UUID:
        row = MarketSnapshotRow(
            id=snapshot.snapshot_id,
            cycle_id=snapshot.cycle_id,
            symbol=snapshot.symbol,
            captured_at=snapshot.captured_at,
            schema_version=snapshot.schema_version,
            last_price=snapshot.last_price,
            data_quality=snapshot.data_quality.value,
            degraded_fields=list(snapshot.degraded_fields),
            context=snapshot_context(snapshot),
            sources=snapshot_sources(snapshot),
        )
        self._session.add(row)
        await self.upsert_candles(snapshot)
        await self._session.flush()
        return snapshot.snapshot_id

    async def upsert_candles(self, snapshot: MarketSnapshot) -> int:
        """Idempotent per (symbol, timeframe, open_time) — re-fetching costs nothing."""
        rows = candle_rows(snapshot)
        if not rows:
            return 0

        statement = insert(OhlcvCandleRow).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["symbol", "timeframe", "open_time"],
            set_={
                "open": statement.excluded.open,
                "high": statement.excluded.high,
                "low": statement.excluded.low,
                "close": statement.excluded.close,
                "volume": statement.excluded.volume,
                "source": statement.excluded.source,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
        await self._session.execute(statement)
        return len(rows)

    async def latest_per_symbol(self, limit: int = 20) -> list[MarketSnapshotRow]:
        """The newest snapshot for each symbol — ``/status``'s data-quality block.

        DISTINCT ON is Postgres-specific and deliberate: the alternative is one
        query per watchlist symbol, and a chat command should not fan out to ten
        round trips to answer one question.
        """
        statement = (
            select(MarketSnapshotRow)
            .distinct(MarketSnapshotRow.symbol)
            .order_by(MarketSnapshotRow.symbol, MarketSnapshotRow.captured_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars())

    async def latest_for_symbol(self, symbol: str) -> MarketSnapshotRow | None:
        result = await self._session.execute(
            select(MarketSnapshotRow)
            .where(MarketSnapshotRow.symbol == symbol)
            .order_by(MarketSnapshotRow.captured_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class InstrumentMetaRepository:
    """24h cache of exchange trading rules."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, meta: InstrumentMeta) -> None:
        statement = insert(InstrumentMetaRow).values(
            symbol=meta.symbol,
            tick_size=meta.tick_size,
            qty_step=meta.qty_step,
            min_notional=meta.min_notional,
            contract_size=meta.contract_size,
            source=meta.source,
            fetched_at=meta.fetched_at,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["symbol"],
                set_={
                    "tick_size": statement.excluded.tick_size,
                    "qty_step": statement.excluded.qty_step,
                    "min_notional": statement.excluded.min_notional,
                    "contract_size": statement.excluded.contract_size,
                    "source": statement.excluded.source,
                    "fetched_at": statement.excluded.fetched_at,
                },
            )
        )

    async def get(self, symbol: str) -> InstrumentMeta | None:
        row = await self._session.get(InstrumentMetaRow, symbol)
        if row is None:
            return None
        return InstrumentMeta(
            source=row.source,
            fetched_at=row.fetched_at,
            symbol=row.symbol,
            tick_size=row.tick_size,
            qty_step=row.qty_step,
            min_notional=row.min_notional,
            contract_size=row.contract_size,
        )


class FxRateRepository:
    """Stores the last-known-good rate for the FX client's fallback."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, rate: FxRate) -> None:
        statement = insert(FxRateRow).values(
            pair=rate.pair, rate=rate.rate, source=rate.source, fetched_at=rate.fetched_at
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["pair"],
                set_={
                    "rate": statement.excluded.rate,
                    "source": statement.excluded.source,
                    "fetched_at": statement.excluded.fetched_at,
                },
            )
        )

    async def get(self, pair: str = "EURUSD") -> FxRate | None:
        row = await self._session.get(FxRateRow, pair)
        if row is None:
            return None
        return FxRate(
            source=row.source,
            fetched_at=row.fetched_at,
            pair=row.pair,
            rate=row.rate,
            is_last_known_good=True,
        )


class GateDecisionRepository:
    """Audit trail for every risk-gate verdict (PRD F10; M9's rejection stats)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def to_row(
        decision: GateDecision, cycle_id: UUID | None = None, *, user_id: int
    ) -> dict[str, Any]:
        """Pure serialization — the reason travels as a code, not just as prose.

        ``user_id`` is required rather than defaulted: the same report can approve
        for one user and reject for another, and a row that cannot say whose verdict
        it is makes those two contradict each other (PRD G5).
        """
        return {
            "cycle_id": cycle_id,
            "user_id": user_id,
            "symbol": decision.symbol,
            "evaluated_at": decision.evaluated_at,
            "gate_status": decision.status.value,
            "reason": None if decision.reason is None else decision.reason.value,
            "message": decision.message[:512],
            "prompt_version": decision.prompt_version,
            "plan": None if decision.plan is None else decision.plan.model_dump(mode="json"),
        }

    async def record(
        self, decision: GateDecision, cycle_id: UUID | None = None, *, user_id: int
    ) -> None:
        self._session.add(GateDecisionRow(**self.to_row(decision, cycle_id, user_id=user_id)))

    async def recent(self, limit: int = 50) -> list[GateDecisionRow]:
        result = await self._session.execute(
            select(GateDecisionRow).order_by(GateDecisionRow.evaluated_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


class RiskStateRepository:
    """§7 — pause state that survives a restart. One row, id=1."""

    ROW_ID = 1

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self) -> PauseState:
        row = await self._session.get(RiskStateRow, self.ROW_ID)
        if row is None:
            return PauseState()
        return PauseState(
            paused=row.paused,
            reason=None if row.pause_reason is None else PauseReason(row.pause_reason),
            until=row.paused_until,
        )

    async def save(self, state: PauseState) -> None:
        statement = insert(RiskStateRow).values(
            id=self.ROW_ID,
            paused=state.paused,
            pause_reason=None if state.reason is None else state.reason.value,
            paused_until=state.until,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "paused": statement.excluded.paused,
                    "pause_reason": statement.excluded.pause_reason,
                    "paused_until": statement.excluded.paused_until,
                },
            )
        )


class IngestionFailureRepository:
    """Audit trail for skipped symbols and degraded sources."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        source: str,
        reason: str,
        occurred_at: datetime,
        symbol: str | None = None,
        cycle_id: UUID | None = None,
    ) -> None:
        statement = insert(IngestionFailureRow).values(
            cycle_id=cycle_id,
            symbol=symbol,
            source=source,
            reason=reason[:512],
            occurred_at=occurred_at,
        )
        await self._session.execute(statement.on_conflict_do_nothing())


def llm_call_row(call: LLMCall) -> dict[str, Any]:
    """Pure serialization for an LLM call — testable without a database."""
    return {
        "id": call.call_id,
        "cycle_id": call.cycle_id,
        "symbol": call.symbol,
        "kind": call.kind.value,
        "provider": call.provider,
        "model": call.model,
        "prompt_version": call.prompt_version,
        "attempt": call.attempt,
        "status": call.status.value,
        "stop_reason": call.stop_reason,
        "refusal_category": call.refusal_category,
        "error": None if call.error is None else call.error[:1024],
        "tokens_in": call.usage.input_tokens,
        "tokens_out": call.usage.output_tokens,
        "cache_read_tokens": call.usage.cache_read_tokens,
        "cache_write_tokens": call.usage.cache_write_tokens,
        "cost_usd_estimate": call.cost_usd_estimate,
        "duration_ms": call.duration_ms,
        "request_id": call.request_id,
        "started_at": call.started_at,
        "request": call.request,
        "response": call.response,
    }


def analyst_report_row(
    report: AnalystReport,
    *,
    created_at: datetime,
    provider: str,
    role: str = "primary",
    cycle_id: UUID | None = None,
    snapshot_id: UUID | None = None,
    llm_call_id: UUID | None = None,
) -> dict[str, Any]:
    """Pure serialization for a validated report."""
    return {
        "cycle_id": cycle_id,
        "snapshot_id": snapshot_id,
        "llm_call_id": llm_call_id,
        "symbol": report.symbol,
        "created_at": created_at,
        "role": role,
        "provider": provider,
        "model": report.model or "",
        "prompt_version": report.prompt_version or "",
        "candidate_status": report.candidate_status.value,
        "setup_type": report.setup_type.value,
        "direction": report.direction.value,
        "confidence": report.confidence,
        "thesis": report.thesis[:1024],
        "report": report.model_dump(mode="json"),
    }


class LLMCallRepository:
    """Audit trail for every LLM call — successes, refusals and failures alike."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, call: LLMCall) -> UUID:
        self._session.add(LLMCallRow(**llm_call_row(call)))
        return call.call_id

    async def record_many(self, calls: Sequence[LLMCall]) -> int:
        for call in calls:
            await self.record(call)
        return len(calls)

    async def spend_totals(
        self, *, day_start: datetime, month_start: datetime, priced_models: Sequence[str]
    ) -> SpendTotals:
        """Estimated spend over the current UTC day and month (M7's spend guard).

        ``unpriced_calls`` counts calls whose model has no entry in
        ``config.llm.pricing``. ``pricing.estimate_cost`` records those at 0 with a
        warning — right for keeping the audit row, and a hole in a spend guard, so
        they are counted here rather than quietly treated as free.
        """

        async def total(since: datetime) -> Decimal:
            statement = select(func.coalesce(func.sum(LLMCallRow.cost_usd_estimate), 0)).where(
                LLMCallRow.started_at >= since
            )
            return Decimal((await self._session.execute(statement)).scalar_one())

        counts = select(
            func.count(),
            func.count().filter(LLMCallRow.model.not_in(priced_models)),
        ).where(LLMCallRow.started_at >= day_start)
        calls, unpriced = (await self._session.execute(counts)).one()

        return SpendTotals(
            day_usd=await total(day_start),
            month_usd=await total(month_start),
            calls=int(calls),
            unpriced_calls=int(unpriced),
        )

    async def recent(self, limit: int = 50) -> list[LLMCallRow]:
        result = await self._session.execute(
            select(LLMCallRow).order_by(LLMCallRow.started_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


class AnalystReportRepository:
    """Stored reports, and the query behind specs/PROMPTS.md §3's history block."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        report: AnalystReport,
        *,
        created_at: datetime,
        provider: str,
        role: str = "primary",
        cycle_id: UUID | None = None,
        snapshot_id: UUID | None = None,
        llm_call_id: UUID | None = None,
    ) -> UUID:
        row = AnalystReportRow(
            **analyst_report_row(
                report,
                created_at=created_at,
                provider=provider,
                role=role,
                cycle_id=cycle_id,
                snapshot_id=snapshot_id,
                llm_call_id=llm_call_id,
            )
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def latest_non_candidates(self, *, since: datetime) -> dict[str, datetime]:
        """Newest non-candidate verdict per symbol inside the window (M8.2).

        Feeds the re-analysis cooldown, which is why it asks for the **newest** row
        per symbol rather than any row: a symbol analysed twice in the window is
        quiet from the later one, and a symbol that has since produced a CANDIDATE
        must not be suppressed by an older WATCHLIST.

        ``role='primary'`` keeps M10's second ensemble provider from counting as a
        separate look at the same symbol.
        """
        newest = (
            select(
                AnalystReportRow.symbol,
                func.max(AnalystReportRow.created_at).label("created_at"),
            )
            .where(AnalystReportRow.created_at >= since, AnalystReportRow.role == "primary")
            .group_by(AnalystReportRow.symbol)
            .subquery()
        )
        statement = (
            select(AnalystReportRow.symbol, newest.c.created_at)
            .join(
                newest,
                (AnalystReportRow.symbol == newest.c.symbol)
                & (AnalystReportRow.created_at == newest.c.created_at),
            )
            .where(AnalystReportRow.candidate_status.in_(("WATCHLIST", "NO_SETUP")))
        )
        rows = (await self._session.execute(statement)).all()
        return {row.symbol: row.created_at for row in rows}

    async def recent_for_symbol(
        self, symbol: str, limit: int = 3, *, owner_id: int, role: str = "primary"
    ) -> list[PastVerdict]:
        """Last N verdicts for one symbol, newest first (specs/PROMPTS.md §3).

        §3 asks for "status + one-line thesis + **what happened**", and from M7
        what happened is knowable. Each report is matched to its cycle's outcome
        for the same symbol — the tracked result if it became a signal, otherwise
        the gate's own verdict, because "the analyst proposed this and the gate
        rejected it for thin net RR" is exactly the feedback §3 exists to give.

        ``(cycle_id, symbol)`` is the join, and it is a LEFT join in effect: a
        report with neither is left ``None``, and the block renders "outcome not
        resolved yet" rather than inventing one.

        **``owner_id`` scopes the outcome (M8.1),** for the same reason
        ``stats.setup_stats`` is owner-scoped: one shared report now has one signal
        row and one gate verdict per user, and "what happened" has to be a single
        answer. The owner's is the one the analyst calibrates against.
        """
        result = await self._session.execute(
            select(AnalystReportRow)
            .where(AnalystReportRow.symbol == symbol, AnalystReportRow.role == role)
            .order_by(AnalystReportRow.created_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
        outcomes = await self._outcomes_for(rows, owner_id=owner_id)
        return [
            PastVerdict(
                created_at=row.created_at,
                candidate_status=CandidateStatus(row.candidate_status),
                setup_type=SetupType(row.setup_type),
                direction=row.direction,
                confidence=row.confidence,
                thesis=row.thesis,
                prompt_version=row.prompt_version,
                outcome=None if row.cycle_id is None else outcomes.get(row.cycle_id),
            )
            for row in rows
        ]

    async def _outcomes_for(
        self, rows: Sequence[AnalystReportRow], *, owner_id: int
    ) -> dict[UUID, str]:
        """What became of each report, keyed by cycle id — in the owner's book."""
        cycles = [row.cycle_id for row in rows if row.cycle_id is not None]
        if not cycles:
            return {}
        symbols = {row.symbol for row in rows}

        found: dict[UUID, str] = {}

        signals = await self._session.execute(
            select(SignalRow).where(
                SignalRow.cycle_id.in_(cycles),
                SignalRow.symbol.in_(symbols),
                SignalRow.user_id == owner_id,
            )
        )
        for signal in signals.scalars():
            if signal.cycle_id is None:  # pragma: no cover — filtered by the query
                continue
            found[signal.cycle_id] = describe_outcome(
                status=signal.status,
                outcome=signal.outcome,
                realized_r=signal.realized_r,
                decision=signal.decision,
                dry_run=signal.dry_run,
            )

        gates = await self._session.execute(
            select(GateDecisionRow).where(
                GateDecisionRow.cycle_id.in_(cycles),
                GateDecisionRow.symbol.in_(symbols),
                GateDecisionRow.user_id == owner_id,
            )
        )
        for gate in gates.scalars():
            if gate.cycle_id is None or gate.cycle_id in found:  # pragma: no cover
                continue
            if gate.gate_status == GateStatus.REJECTED.value and gate.reason:
                found[gate.cycle_id] = f"rejected at the gate [{gate.reason}]"
            elif gate.gate_status == GateStatus.DOWNGRADED_WATCHLIST.value:
                found[gate.cycle_id] = "downgraded to watchlist at the gate"
        return found


def describe_outcome(
    *,
    status: str,
    outcome: str | None,
    realized_r: Decimal | None,
    decision: str | None,
    dry_run: bool,
) -> str:
    """One line of "what happened" for the analyst's history block.

    Deliberately terse and deliberately labelled. A dry-run result says so, so the
    model is never told a rehearsal was a trade; and an open signal says it is
    open rather than reporting the R it happens to be showing right now, which
    would teach the analyst to read an unrealised number as a result.
    """
    from sentinel.bot.models import OPEN_STATUSES, SignalStatus

    prefix = "dry run: " if dry_run else ""
    if SignalStatus(status) in OPEN_STATUSES:
        return f"{prefix}signal open ({status.lower().replace('_', ' ')})"

    taken = " (taken)" if decision == SignalDecision.TAKEN.value else ""
    if outcome in {"EXPIRY", "INVALIDATION"}:
        word = "expired unfilled" if outcome == "EXPIRY" else "invalidated before entry"
        return f"{prefix}{word}{taken}"
    if realized_r is None:
        return f"{prefix}closed{taken}"
    label = (outcome or "closed").lower()
    return f"{prefix}{label}, {realized_r:+.2f}R{taken}"


#: The outcomes that arm a per-symbol cooldown (ARCHITECTURE §3, M7's ruling).
#: A signal that reached its targets is not a reason to stay away from the symbol.
_COOLDOWN_ARMING = (
    SignalStatus.STOPPED.value,
    SignalStatus.EXPIRED.value,
    SignalStatus.INVALIDATED.value,
)


def signal_row(record: SignalRecord, plan: TradePlan) -> dict[str, Any]:
    """Pure: a ``SignalRecord`` as column values. Testable without a database."""
    return {
        "id": record.signal_id,
        "plan_id": plan.plan_id,
        "cycle_id": record.cycle_id,
        "user_id": record.user_id,
        "symbol": plan.symbol,
        "direction": plan.direction.value,
        "setup_type": plan.setup_type.value,
        "prompt_version": plan.report.prompt_version,
        "confidence": plan.confidence,
        "created_at": plan.created_at,
        "expires_at": plan.expires_at,
        "status": record.status.value,
        "decision": None if record.decision is None else record.decision.value,
        "decided_at": record.decided_at,
        "decided_by_user_id": record.decided_by_user_id,
        "plan": plan.model_dump(mode="json"),
        "chart_params": list(record.chart_params),
        "dry_run": record.dry_run,
    }


class SignalRepository:
    """Signals as delivered to Telegram (ARCHITECTURE.md §3 contract 5).

    **Every query that reads a book takes ``user_id`` as a required keyword** (M8.1).
    Not an optional one defaulting to "everybody": the whole promise of per-user
    statistics is that one user's REAL population can never contain another user's
    decision, and a default that silently means "all users" turns that promise into
    something each caller has to remember. A forgotten filter is now a
    ``mypy --strict`` error instead of a privacy leak.

    The three ``*_by_user`` methods are the exception, and they exist for the
    pre-analyst guard: it needs every eligible user's state in one pass, because it
    runs before a $0.32 call and once per cycle rather than once per user.
    ``open_signals`` is the other exception and is deliberately global — the tracker
    follows everybody's signals, and each row carries the ``user_id`` that decides
    where its consequences land.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, record: SignalRecord) -> SignalRecord | None:
        """Insert the signal, or return ``None`` if this plan already has one.

        ``plan_id`` is unique, so a re-published plan — after a restart, a retry,
        or a duplicated cycle — collides here rather than reaching Telegram twice
        (specs/TELEGRAM_UX.md §6). ``None`` means "someone already owns this".
        """
        statement = (
            insert(SignalRow)
            .values(signal_row(record, record.plan))
            .on_conflict_do_nothing(index_elements=["plan_id"])
            .returning(SignalRow.id, SignalRow.number)
        )
        row = (await self._session.execute(statement)).first()
        if row is None:
            return None
        return record.model_copy(update={"signal_id": row.id, "number": row.number})

    async def get_by_plan_id(self, plan_id: UUID) -> SignalRow | None:
        statement = select(SignalRow).where(SignalRow.plan_id == plan_id)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get(self, signal_id: UUID) -> SignalRow | None:
        return await self._session.get(SignalRow, signal_id)

    async def record_decision(
        self,
        signal_id: UUID,
        decision: SignalDecision,
        *,
        at: datetime,
        user_id: int,
    ) -> tuple[SignalRow, bool] | None:
        """Persist a button press, and say whether it changed anything.

        Idempotent: pressing the same button twice writes nothing and reports
        ``changed=False``, so the caller can skip an edit Telegram would reject
        anyway. ``None`` means there is no such signal.
        """
        row = await self._session.get(SignalRow, signal_id)
        if row is None:
            return None
        if row.decision == decision.value:
            return row, False
        row.decision = decision.value
        row.decided_at = at
        row.decided_by_user_id = user_id
        return row, True

    async def with_decision(
        self, decision: SignalDecision, *, user_id: int, limit: int = 50
    ) -> list[SignalRow]:
        """One user's signals marked a given way, newest first (``/positions``)."""
        statement = (
            select(SignalRow)
            .where(SignalRow.user_id == user_id, SignalRow.decision == decision.value)
            .order_by(SignalRow.created_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars())

    async def recent(self, *, user_id: int, limit: int = 20) -> list[SignalRow]:
        statement = (
            select(SignalRow)
            .where(SignalRow.user_id == user_id)
            .order_by(SignalRow.created_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars())

    async def undecided_count(self, *, user_id: int) -> int:
        statement = (
            select(func.count())
            .select_from(SignalRow)
            .where(SignalRow.user_id == user_id, SignalRow.decision.is_(None))
        )
        return int((await self._session.execute(statement)).scalar_one())

    # ---- M7 ----------------------------------------------------------------

    async def open_signals(self) -> list[SignalRow]:
        """Every signal the tracker is still following, oldest first.

        Every decision, including none at all: specs/TELEGRAM_UX.md §2 resolves a
        skipped signal's outcome too, "because what skipping costs is itself a
        measurement", and a signal the owner has not answered yet still fills and
        still stops.

        This is also the whole of crash recovery. The tracker keeps no state
        between ticks, so a restart simply asks this question again.
        """
        statement = (
            select(SignalRow)
            .where(SignalRow.status.in_([status.value for status in OPEN_STATUSES]))
            .order_by(SignalRow.created_at)
        )
        return list((await self._session.execute(statement)).scalars())

    async def open_symbols(self, *, user_id: int) -> set[str]:
        """One user's symbols with a live signal — PRD F11's "max 1 per symbol".

        Per user, and that matters: under one shared analysis, a symbol another user
        already holds must not stop this user from being offered it.
        """
        statement = select(SignalRow.symbol).where(
            SignalRow.user_id == user_id,
            SignalRow.status.in_([status.value for status in OPEN_STATUSES]),
        )
        return set((await self._session.execute(statement)).scalars())

    async def open_symbols_by_user(self) -> dict[int, set[str]]:
        """``{user_id: open symbols}`` for the pre-analyst guard, in one pass."""
        statement = select(SignalRow.user_id, SignalRow.symbol).where(
            SignalRow.status.in_([status.value for status in OPEN_STATUSES])
        )
        grouped: dict[int, set[str]] = {}
        for row in (await self._session.execute(statement)).all():
            grouped.setdefault(row.user_id, set()).add(row.symbol)
        return grouped

    async def open_taken(self, *, user_id: int) -> list[SignalRow]:
        """Live signals this user actually took — their open-risk budget (§2 rule 7).

        Watched, skipped and dry-run signals are tracked but commit nothing, so
        they cannot occupy a budget the user never spent.
        """
        statement = select(SignalRow).where(
            SignalRow.user_id == user_id,
            SignalRow.status.in_([status.value for status in OPEN_STATUSES]),
            SignalRow.decision == SignalDecision.TAKEN.value,
            SignalRow.dry_run.is_(False),
        )
        return list((await self._session.execute(statement)).scalars())

    async def published_since(self, since: datetime, *, user_id: int) -> int:
        """This user's signals since an instant — specs/TELEGRAM_UX.md §6's cap.

        Per user, because the cap is about how much the system talks to one person.
        Dry-run signals count: the cap exists to stop the system talking too much,
        and a rehearsal day that ignored it would not rehearse the guard.
        """
        statement = (
            select(func.count())
            .select_from(SignalRow)
            .where(SignalRow.user_id == user_id, SignalRow.created_at >= since)
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def published_by_user_since(self, since: datetime) -> dict[int, int]:
        """``{user_id: count}`` since an instant, for the pre-analyst guard."""
        statement = (
            select(SignalRow.user_id, func.count())
            .where(SignalRow.created_at >= since)
            .group_by(SignalRow.user_id)
        )
        return {row[0]: int(row[1]) for row in (await self._session.execute(statement)).all()}

    async def resolutions_since(
        self, since: datetime, *, user_id: int
    ) -> list[tuple[str, datetime]]:
        """``(symbol, closed_at)`` for this user's signals resolved since an instant.

        Feeds ``rails.cooldown_until``. Only outcomes that *arm* a cooldown are
        returned: a stop-out, an expiry and an invalidation. A signal that reached
        its targets is not a reason to stay away from the symbol.
        """
        statement = select(SignalRow.symbol, SignalRow.closed_at).where(
            SignalRow.user_id == user_id,
            SignalRow.status.in_(_COOLDOWN_ARMING),
            SignalRow.closed_at.is_not(None),
            SignalRow.closed_at >= since,
        )
        rows = (await self._session.execute(statement)).all()
        return [(row.symbol, row.closed_at) for row in rows if row.closed_at is not None]

    async def resolutions_by_user_since(
        self, since: datetime
    ) -> dict[int, list[tuple[str, datetime]]]:
        """``{user_id: [(symbol, closed_at)]}``, for the pre-analyst guard."""
        statement = select(SignalRow.user_id, SignalRow.symbol, SignalRow.closed_at).where(
            SignalRow.status.in_(_COOLDOWN_ARMING),
            SignalRow.closed_at.is_not(None),
            SignalRow.closed_at >= since,
        )
        grouped: dict[int, list[tuple[str, datetime]]] = {}
        for row in (await self._session.execute(statement)).all():
            if row.closed_at is not None:
                grouped.setdefault(row.user_id, []).append((row.symbol, row.closed_at))
        return grouped

    async def realized_eur_by_user_since(self, since: datetime) -> dict[int, list[Decimal]]:
        """``{user_id: signed realized P&L}`` for taken signals closed since (§7).

        Grouped rather than per-user because the tracker's daily-loss rail has to
        ask the question for everybody on every tick, and it does not otherwise need
        to know who the users are. Dry-run excluded: a paper loss cannot pause a
        real account.
        """
        statement = select(SignalRow.user_id, SignalRow.realized_eur).where(
            SignalRow.decision == SignalDecision.TAKEN.value,
            SignalRow.dry_run.is_(False),
            SignalRow.closed_at.is_not(None),
            SignalRow.closed_at >= since,
            SignalRow.realized_eur.is_not(None),
        )
        grouped: dict[int, list[Decimal]] = {}
        for row in (await self._session.execute(statement)).all():
            if row.realized_eur is not None:
                grouped.setdefault(row.user_id, []).append(row.realized_eur)
        return grouped

    async def resolved_since(
        self, since: datetime | None = None, *, user_id: int
    ) -> list[SignalRow]:
        """One user's signals with a measured outcome — the /stats population."""
        statement = select(SignalRow).where(
            SignalRow.user_id == user_id, SignalRow.closed_at.is_not(None)
        )
        if since is not None:
            statement = statement.where(SignalRow.closed_at >= since)
        return list(
            (await self._session.execute(statement.order_by(SignalRow.closed_at))).scalars()
        )

    async def advance(self, signal_id: UUID, **fields: Any) -> SignalRow | None:
        """Write the tracker's roll-up columns onto a signal.

        Deliberately a field bag rather than a fixed signature: which columns a
        tick touches depends on what the market did, and enumerating every
        combination here would be a worse contract than one the caller states at
        the call site. Unknown keys raise rather than being silently dropped.
        """
        row = await self._session.get(SignalRow, signal_id)
        if row is None:
            return None
        for key, value in fields.items():
            if not hasattr(row, key):
                raise AttributeError(f"signals has no column {key!r}")
            setattr(row, key, value)
        return row


class TelegramMessageRepository:
    """specs/TELEGRAM_UX.md §6 — message ids stored, restarts never double-post.

    Claim-then-send, in two steps, because there is no third option that is safe:
    claiming after the send loses the id if the process dies in between, and not
    claiming at all means every restart re-posts.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        signal_id: UUID,
        kind: MessageKind,
        chat_id: int,
        *,
        at: datetime,
        event_key: str = "",
    ) -> bool:
        """Reserve ``(signal, kind, chat)``. ``False`` means someone already has it.

        A ``False`` here is the entire anti-double-post guarantee: it is returned
        both for a message already sent and for one claimed but never confirmed
        (a crash between send and commit). The stuck claim is deliberately *not*
        retried — a duplicated signal card is worse than a missing one the owner
        can ask for again — and ``/status`` reports it so it is never silent.
        """
        statement = (
            insert(TelegramMessageRow)
            .values(
                signal_id=signal_id,
                kind=kind.value,
                chat_id=chat_id,
                event_key=event_key,
                status=MessageStatus.PENDING.value,
                claimed_at=at,
            )
            .on_conflict_do_nothing(index_elements=["signal_id", "kind", "chat_id", "event_key"])
            .returning(TelegramMessageRow.id)
        )
        return (await self._session.execute(statement)).first() is not None

    async def confirm(
        self,
        signal_id: UUID,
        kind: MessageKind,
        chat_id: int,
        *,
        message_id: int,
        at: datetime,
        event_key: str = "",
    ) -> None:
        row = await self._row(signal_id, kind, chat_id, event_key)
        if row is None:  # pragma: no cover — confirm always follows a successful claim
            return
        row.message_id = message_id
        row.status = MessageStatus.SENT.value
        row.sent_at = at
        row.error = None

    async def fail(
        self,
        signal_id: UUID,
        kind: MessageKind,
        chat_id: int,
        *,
        error: str,
        event_key: str = "",
    ) -> None:
        row = await self._row(signal_id, kind, chat_id, event_key)
        if row is None:  # pragma: no cover — fail always follows a successful claim
            return
        row.status = MessageStatus.FAILED.value
        row.error = error[:512]

    async def get(
        self, signal_id: UUID, kind: MessageKind, chat_id: int, event_key: str = ""
    ) -> PostedMessage | None:
        row = await self._row(signal_id, kind, chat_id, event_key)
        if row is None:
            return None
        return PostedMessage(
            signal_id=row.signal_id,
            kind=MessageKind(row.kind),
            chat_id=row.chat_id,
            event_key=row.event_key,
            message_id=row.message_id,
            status=MessageStatus(row.status),
            error=row.error,
        )

    async def claimed_message(self, chat_id: int, message_id: int) -> tuple[UUID, str] | None:
        """Which signal and event a delivered message belongs to.

        The reverse lookup behind the manage prompts: a reply carries the id of the
        message it answers, and that is enough to recover the question without any
        in-memory conversation state to lose on restart.
        """
        statement = select(TelegramMessageRow).where(
            TelegramMessageRow.chat_id == chat_id,
            TelegramMessageRow.message_id == message_id,
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else (row.signal_id, row.event_key)

    async def stuck(self) -> list[TelegramMessageRow]:
        """Claims that never reached ``SENT`` — surfaced by ``/status``."""
        statement = select(TelegramMessageRow).where(
            TelegramMessageRow.status != MessageStatus.SENT.value
        )
        return list((await self._session.execute(statement)).scalars())

    async def _row(
        self, signal_id: UUID, kind: MessageKind, chat_id: int, event_key: str = ""
    ) -> TelegramMessageRow | None:
        statement = select(TelegramMessageRow).where(
            TelegramMessageRow.signal_id == signal_id,
            TelegramMessageRow.kind == kind.value,
            TelegramMessageRow.chat_id == chat_id,
            TelegramMessageRow.event_key == event_key,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()


class RuntimeSettingsRepository:
    """ARCHITECTURE.md §2's "DB" layer: DB > yaml > defaults.

    Every write also appends to ``config_changes`` — PRD F10 stores config changes,
    not just current values, so "why was this signal sized against €8,000" has a
    timestamped answer instead of an inference.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def all(self) -> dict[str, Any]:
        rows = (await self._session.execute(select(RuntimeSettingRow))).scalars()
        return {row.key: row.value for row in rows}

    async def get(self, key: str) -> Any | None:
        row = await self._session.get(RuntimeSettingRow, key)
        return None if row is None else row.value

    async def set(self, key: str, value: Any, *, at: datetime, user_id: int | None) -> None:
        previous = await self.get(key)
        statement = insert(RuntimeSettingRow).values(
            key=key, value=value, updated_at=at, updated_by_user_id=user_id
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": statement.excluded.value,
                    "updated_at": statement.excluded.updated_at,
                    "updated_by_user_id": statement.excluded.updated_by_user_id,
                },
            )
        )
        self._session.add(
            ConfigChangeRow(
                key=key,
                old_value=previous,
                new_value=value,
                actor_user_id=user_id,
                changed_at=at,
            )
        )

    async def changes(self, limit: int = 50) -> list[ConfigChangeRow]:
        statement = select(ConfigChangeRow).order_by(ConfigChangeRow.changed_at.desc()).limit(limit)
        return list((await self._session.execute(statement)).scalars())


def _as_json(value: Decimal | None) -> str | None:
    """Decimals are stored as strings in JSONB here, as everywhere else."""
    return None if value is None else str(value)


def user_account(row: UserRow) -> UserAccount:
    """``users`` row → contract. Pure, so it is testable without a database."""
    return UserAccount(
        telegram_user_id=row.telegram_user_id,
        status=UserStatus(row.status),
        role=UserRole(row.role),
        username=row.username,
        display_name=row.display_name,
        requested_at=row.requested_at,
        decided_at=row.decided_at,
        decided_by_user_id=row.decided_by_user_id,
        capital_eur=row.capital_eur,
        risk_per_trade_pct=row.risk_per_trade_pct,
        acknowledged_at=row.acknowledged_at,
        acknowledged_version=row.acknowledged_version,
        pause=PauseState(
            paused=row.paused,
            reason=None if row.pause_reason is None else PauseReason(row.pause_reason),
            until=row.paused_until,
        ),
        notice_at=row.notice_at,
    )


class UserRepository:
    """Who may talk to this bot, and what each of them is sized against (M8.1).

    ``request`` is the only method a stranger can reach, and it is the whole
    anti-abuse story: ``ON CONFLICT DO NOTHING`` on the primary key means a second
    ``/start`` writes nothing and returns ``None``, so the owner is notified exactly
    once per id no matter how many times the button is pressed. That guarantee is a
    constraint rather than a counter, which is why a restart cannot lose it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> UserAccount | None:
        row = await self._session.get(UserRow, user_id)
        return None if row is None else user_account(row)

    async def owner(self) -> UserAccount | None:
        statement = select(UserRow).where(UserRow.role == UserRole.OWNER.value)
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else user_account(row)

    async def all(self) -> list[UserAccount]:
        """Everyone, oldest request first — the order ``/users`` reads in."""
        statement = select(UserRow).order_by(UserRow.requested_at, UserRow.telegram_user_id)
        return [user_account(row) for row in (await self._session.execute(statement)).scalars()]

    async def approved(self) -> list[UserAccount]:
        """The fan-out list, ordered by id so a cycle is reproducible."""
        statement = (
            select(UserRow)
            .where(UserRow.status == UserStatus.APPROVED.value)
            .order_by(UserRow.telegram_user_id)
        )
        return [user_account(row) for row in (await self._session.execute(statement)).scalars()]

    async def request(
        self,
        user_id: int,
        *,
        username: str | None,
        display_name: str | None,
        at: datetime,
    ) -> UserAccount | None:
        """Create a PENDING row. ``None`` means this id has already asked.

        Returning ``None`` for an existing row — in any state — is what makes a
        re-request a no-op: a rejected stranger pressing ``/start`` again neither
        reopens their case nor reaches the owner.
        """
        statement = (
            insert(UserRow)
            .values(
                telegram_user_id=user_id,
                username=username,
                display_name=display_name,
                status=UserStatus.PENDING.value,
                role=UserRole.MEMBER.value,
                requested_at=at,
                acknowledged_version="",
                paused=False,
                updated_at=at,
            )
            .on_conflict_do_nothing(index_elements=["telegram_user_id"])
            .returning(UserRow)
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else user_account(row)

    async def ensure_owner(
        self,
        user_id: int,
        *,
        at: datetime,
        acknowledged_version: str,
    ) -> UserAccount:
        """Seed the OWNER row on a database that has none (a fresh install).

        Idempotent, and deliberately *not* an upsert: it never overwrites an
        existing row, so it cannot silently re-approve an owner who suspended
        themselves or reset a capital they changed. Migration 0007 does the same
        thing for a database that already had data; this covers the one it cannot,
        which is an empty one.
        """
        await self._session.execute(
            insert(UserRow)
            .values(
                telegram_user_id=user_id,
                status=UserStatus.APPROVED.value,
                role=UserRole.OWNER.value,
                requested_at=at,
                decided_at=at,
                decided_by_user_id=user_id,
                acknowledged_at=at,
                acknowledged_version=acknowledged_version,
                paused=False,
                updated_at=at,
            )
            .on_conflict_do_nothing(index_elements=["telegram_user_id"])
        )
        seeded = await self.get(user_id)
        assert seeded is not None  # the insert either created it or it was there
        return seeded

    async def set_status(
        self,
        user_id: int,
        status: UserStatus,
        *,
        at: datetime,
        by_user_id: int | None,
    ) -> UserAccount | None:
        """Approve, reject, suspend — or record that a member left.

        ``None`` when the id is unknown, so a mistyped ``/approve 12345`` says so
        instead of appearing to work.
        """
        row = await self._session.get(UserRow, user_id)
        if row is None:
            return None
        row.status = status.value
        row.decided_at = at
        row.decided_by_user_id = by_user_id
        row.updated_at = at
        return user_account(row)

    async def acknowledge(self, user_id: int, *, version: str, at: datetime) -> UserAccount | None:
        row = await self._session.get(UserRow, user_id)
        if row is None:  # pragma: no cover — the middleware loaded the row already
            return None
        row.acknowledged_at = at
        row.acknowledged_version = version
        row.updated_at = at
        return user_account(row)

    async def set_capital(
        self, user_id: int, capital_eur: Decimal, *, at: datetime
    ) -> UserAccount | None:
        return await self._set_audited(user_id, "capital_eur", capital_eur, at=at)

    async def set_risk_pct(
        self, user_id: int, risk_per_trade_pct: Decimal, *, at: datetime
    ) -> UserAccount | None:
        return await self._set_audited(user_id, "risk_per_trade_pct", risk_per_trade_pct, at=at)

    async def _set_audited(
        self, user_id: int, column: str, value: Decimal, *, at: datetime
    ) -> UserAccount | None:
        """Write a sizing input, and append the ``config_changes`` row PRD F10 wants.

        These two values left ``runtime_settings`` at M8.1, and
        ``RuntimeSettingsRepository.set`` was where the audit row came from. F10
        stores config *changes*, not merely current values, so that "why was this
        signal sized against €8,000" has a timestamped answer rather than an
        inference — losing that for the only two settings that decide a position
        size would be the wrong half of the table to stop auditing.

        The key is namespaced per user (``user.<id>.capital_eur``) because the audit
        table is shared and a bare ``capital_eur`` would now be ambiguous — and would
        read as a change to the pre-M8.1 global setting, which still has rows there.
        """
        previous = await self.get(user_id)
        if previous is None:  # pragma: no cover — callers hold a loaded account
            return None
        updated = await self._set(user_id, at=at, **{column: value})
        self._session.add(
            ConfigChangeRow(
                key=f"user.{user_id}.{column}",
                old_value=_as_json(getattr(previous, column)),
                new_value=str(value),
                actor_user_id=user_id,
                changed_at=at,
            )
        )
        return updated

    async def set_pause(self, user_id: int, state: PauseState, *, at: datetime) -> None:
        """This user's daily-loss pause (specs/RISK_ENGINE.md §7).

        Not the operator's ``/pause``, which is still ``risk_state``'s single row
        and gates everybody.
        """
        await self._set(
            user_id,
            at=at,
            paused=state.paused,
            pause_reason=None if state.reason is None else state.reason.value,
            paused_until=state.until,
        )

    async def touch_notice(self, user_id: int, *, at: datetime) -> None:
        """Record that this id has just been told where it stands."""
        await self._set(user_id, at=at, notice_at=at)

    async def _set(self, user_id: int, *, at: datetime, **fields: Any) -> UserAccount | None:
        row = await self._session.get(UserRow, user_id)
        if row is None:  # pragma: no cover — callers hold a loaded account
            return None
        for key, value in fields.items():
            if not hasattr(row, key):
                raise AttributeError(f"users has no column {key!r}")
            setattr(row, key, value)
        row.updated_at = at
        return user_account(row)


class CycleRepository:
    """One row per scan cycle (ARCHITECTURE.md §3, PRD G4).

    ``start`` commits nothing on its own — the caller owns the transaction, as
    everywhere else here — but the orchestrator commits it immediately, because a
    cycle row written only at the end would be missing for exactly the cycles that
    matter: the ones that crashed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(self, cycle_id: UUID, *, at: datetime, dry_run: bool, symbols: int) -> None:
        self._session.add(
            CycleRow(
                cycle_id=cycle_id,
                started_at=at,
                status="RUNNING",
                dry_run=dry_run,
                symbols_requested=symbols,
            )
        )

    async def finish(
        self, cycle_id: UUID, *, at: datetime, status: str = "OK", **counts: Any
    ) -> CycleRow | None:
        row = await self._session.get(CycleRow, cycle_id)
        if row is None:  # pragma: no cover — finish always follows start
            return None
        row.finished_at = at
        row.status = status
        for key, value in counts.items():
            if not hasattr(row, key):
                raise AttributeError(f"cycles has no column {key!r}")
            setattr(row, key, value)
        return row

    async def latest(self) -> CycleRow | None:
        """The newest cycle — how ``/health`` and ``/status`` survive a restart."""
        statement = select(CycleRow).order_by(CycleRow.started_at.desc()).limit(1)
        return (await self._session.execute(statement)).scalars().first()

    async def recent(self, limit: int = 10) -> list[CycleRow]:
        """The newest cycles, newest first — M8's failure-streak alert reads this.

        Ordered by ``started_at`` and not ``finished_at``: a cycle that never
        finished has no ``finished_at`` at all, and those are precisely the rows
        the alert exists to notice.
        """
        statement = select(CycleRow).order_by(CycleRow.started_at.desc()).limit(limit)
        return list((await self._session.execute(statement)).scalars())

    async def latest_completed_at(self) -> datetime | None:
        statement = (
            select(CycleRow.finished_at)
            .where(CycleRow.finished_at.is_not(None))
            .order_by(CycleRow.finished_at.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def completion_since(self, since: datetime) -> tuple[int, int]:
        """``(completed, started)`` — the ratio PRD G4 sets at ≥99%."""
        started = select(func.count()).select_from(CycleRow).where(CycleRow.started_at >= since)
        completed = started.where(CycleRow.status == "OK")
        return (
            int((await self._session.execute(completed)).scalar_one()),
            int((await self._session.execute(started)).scalar_one()),
        )


class SignalFillRepository:
    """Entry rungs that actually filled (RISK_ENGINE §3's ladder metadata)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        signal_id: UUID,
        *,
        rung_index: int,
        price: Decimal,
        qty: Decimal,
        filled_at: datetime,
        detected_at: datetime,
        source: str = "tracker",
    ) -> bool:
        """Record a fill once. ``False`` means this rung was already recorded.

        The insert is conflict-tolerant rather than checked-then-inserted: a tick
        re-run after a crash replays the same detection, and the constraint is a
        better guarantee than a read the next tick could race.
        """
        statement = (
            insert(SignalFillRow)
            .values(
                signal_id=signal_id,
                rung_index=rung_index,
                price=price,
                qty=qty,
                filled_at=filled_at,
                detected_at=detected_at,
                source=source,
            )
            .on_conflict_do_nothing(index_elements=["signal_id", "rung_index"])
            .returning(SignalFillRow.id)
        )
        return (await self._session.execute(statement)).first() is not None

    async def for_signal(self, signal_id: UUID) -> list[SignalFillRow]:
        statement = (
            select(SignalFillRow)
            .where(SignalFillRow.signal_id == signal_id)
            .order_by(SignalFillRow.rung_index)
        )
        return list((await self._session.execute(statement)).scalars())

    async def for_signals(self, signal_ids: Sequence[UUID]) -> dict[UUID, list[SignalFillRow]]:
        """One query for a whole tick, rather than one per signal."""
        if not signal_ids:
            return {}
        statement = (
            select(SignalFillRow)
            .where(SignalFillRow.signal_id.in_(signal_ids))
            .order_by(SignalFillRow.rung_index)
        )
        grouped: dict[UUID, list[SignalFillRow]] = {}
        for row in (await self._session.execute(statement)).scalars():
            grouped.setdefault(row.signal_id, []).append(row)
        return grouped


class SignalExitRepository:
    """Closes: a target, the stop, an invalidation, an expiry, a manual exit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        signal_id: UUID,
        *,
        kind: str,
        price: Decimal,
        qty: Decimal,
        exited_at: datetime,
        detected_at: datetime,
    ) -> bool:
        statement = (
            insert(SignalExitRow)
            .values(
                signal_id=signal_id,
                kind=kind,
                price=price,
                qty=qty,
                exited_at=exited_at,
                detected_at=detected_at,
            )
            .on_conflict_do_nothing(index_elements=["signal_id", "kind"])
            .returning(SignalExitRow.id)
        )
        return (await self._session.execute(statement)).first() is not None

    async def for_signal(self, signal_id: UUID) -> list[SignalExitRow]:
        statement = (
            select(SignalExitRow)
            .where(SignalExitRow.signal_id == signal_id)
            .order_by(SignalExitRow.exited_at)
        )
        return list((await self._session.execute(statement)).scalars())

    async def for_signals(self, signal_ids: Sequence[UUID]) -> dict[UUID, list[SignalExitRow]]:
        if not signal_ids:
            return {}
        statement = (
            select(SignalExitRow)
            .where(SignalExitRow.signal_id.in_(signal_ids))
            .order_by(SignalExitRow.exited_at)
        )
        grouped: dict[UUID, list[SignalExitRow]] = {}
        for row in (await self._session.execute(statement)).scalars():
            grouped.setdefault(row.signal_id, []).append(row)
        return grouped


class SignalEventRepository:
    """The tracker's journal, and the queue its notifier drains.

    ``record`` is idempotent on ``(signal_id, event_key)``; ``unposted`` returns
    the events with no ``telegram_messages`` row for a chat yet. Between them, a
    crash anywhere in a tick resolves forward: the event is either not recorded
    (and will be re-derived from the same market data next tick) or recorded and
    not yet posted (and will be posted next tick). There is no third state in
    which it is both lost and believed sent.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        signal_id: UUID,
        *,
        event_key: str,
        kind: str,
        at: datetime,
        from_status: str | None = None,
        to_status: str | None = None,
        price: Decimal | None = None,
        realized_r: Decimal | None = None,
        realized_eur: Decimal | None = None,
        payload: dict[str, Any] | None = None,
        detail: str = "",
    ) -> bool:
        statement = (
            insert(SignalEventRow)
            .values(
                signal_id=signal_id,
                event_key=event_key,
                kind=kind,
                at=at,
                from_status=from_status,
                to_status=to_status,
                price=price,
                realized_r=realized_r,
                realized_eur=realized_eur,
                payload=payload or {},
                detail=detail[:512],
            )
            .on_conflict_do_nothing(index_elements=["signal_id", "event_key"])
            .returning(SignalEventRow.id)
        )
        return (await self._session.execute(statement)).first() is not None

    async def for_signal(self, signal_id: UUID) -> list[SignalEventRow]:
        statement = (
            select(SignalEventRow)
            .where(SignalEventRow.signal_id == signal_id)
            .order_by(SignalEventRow.at, SignalEventRow.id)
        )
        return list((await self._session.execute(statement)).scalars())

    async def unposted(
        self, chat_id: int, *, user_id: int, limit: int = 100
    ) -> list[SignalEventRow]:
        """This user's events with no delivered-or-claimed message, oldest first.

        A LEFT JOIN rather than a status column on the event: the event is a fact
        about the market and the message is a fact about Telegram, and a chat added
        later should receive the events it missed without the event row changing.

        **``user_id`` is what stops the join leaking.** Through M8 "unposted" meant
        "no message row for this chat", which for a single owner was the same thing
        as "mine". With several users it is not: every member would receive a fill
        and stop-out commentary for every other member's signals, because none of
        those events had a message row in *their* chat either. The subquery restricts
        the candidates to signals belonging to this user first.
        """
        mine = select(SignalRow.id).where(SignalRow.user_id == user_id)
        posted = select(TelegramMessageRow.signal_id, TelegramMessageRow.event_key).where(
            TelegramMessageRow.chat_id == chat_id,
            TelegramMessageRow.kind == MessageKind.UPDATE.value,
        )
        statement = (
            select(SignalEventRow)
            .where(
                SignalEventRow.signal_id.in_(mine),
                tuple_(SignalEventRow.signal_id, SignalEventRow.event_key).not_in(posted),
            )
            .order_by(SignalEventRow.at, SignalEventRow.id)
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars())
