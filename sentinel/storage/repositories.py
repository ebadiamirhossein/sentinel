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
    def to_row(decision: GateDecision, cycle_id: UUID | None = None) -> dict[str, Any]:
        """Pure serialization — the reason travels as a code, not just as prose."""
        return {
            "cycle_id": cycle_id,
            "symbol": decision.symbol,
            "evaluated_at": decision.evaluated_at,
            "gate_status": decision.status.value,
            "reason": None if decision.reason is None else decision.reason.value,
            "message": decision.message[:512],
            "prompt_version": decision.prompt_version,
            "plan": None if decision.plan is None else decision.plan.model_dump(mode="json"),
        }

    async def record(self, decision: GateDecision, cycle_id: UUID | None = None) -> None:
        self._session.add(GateDecisionRow(**self.to_row(decision, cycle_id)))

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

    async def recent_for_symbol(
        self, symbol: str, limit: int = 3, *, role: str = "primary"
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
        """
        result = await self._session.execute(
            select(AnalystReportRow)
            .where(AnalystReportRow.symbol == symbol, AnalystReportRow.role == role)
            .order_by(AnalystReportRow.created_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
        outcomes = await self._outcomes_for(rows)
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

    async def _outcomes_for(self, rows: Sequence[AnalystReportRow]) -> dict[UUID, str]:
        """What became of each report, keyed by cycle id."""
        cycles = [row.cycle_id for row in rows if row.cycle_id is not None]
        if not cycles:
            return {}
        symbols = {row.symbol for row in rows}

        found: dict[UUID, str] = {}

        signals = await self._session.execute(
            select(SignalRow).where(SignalRow.cycle_id.in_(cycles), SignalRow.symbol.in_(symbols))
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
                GateDecisionRow.cycle_id.in_(cycles), GateDecisionRow.symbol.in_(symbols)
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


def signal_row(record: SignalRecord, plan: TradePlan) -> dict[str, Any]:
    """Pure: a ``SignalRecord`` as column values. Testable without a database."""
    return {
        "id": record.signal_id,
        "plan_id": plan.plan_id,
        "cycle_id": record.cycle_id,
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
    """Signals as delivered to Telegram (ARCHITECTURE.md §3 contract 5)."""

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

    async def with_decision(self, decision: SignalDecision, *, limit: int = 50) -> list[SignalRow]:
        """Signals the owner marked a given way, newest first (``/positions``)."""
        statement = (
            select(SignalRow)
            .where(SignalRow.decision == decision.value)
            .order_by(SignalRow.created_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars())

    async def recent(self, limit: int = 20) -> list[SignalRow]:
        statement = select(SignalRow).order_by(SignalRow.created_at.desc()).limit(limit)
        return list((await self._session.execute(statement)).scalars())

    async def undecided_count(self) -> int:
        statement = select(func.count()).select_from(SignalRow).where(SignalRow.decision.is_(None))
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

    async def open_symbols(self) -> set[str]:
        """Symbols with a live signal — PRD F11's "max 1 active signal per symbol"."""
        statement = select(SignalRow.symbol).where(
            SignalRow.status.in_([status.value for status in OPEN_STATUSES])
        )
        return set((await self._session.execute(statement)).scalars())

    async def open_taken(self) -> list[SignalRow]:
        """Live signals the owner actually took — the open-risk budget (§2 rule 7).

        Watched, skipped and dry-run signals are tracked but commit nothing, so
        they cannot occupy a budget the owner never spent.
        """
        statement = select(SignalRow).where(
            SignalRow.status.in_([status.value for status in OPEN_STATUSES]),
            SignalRow.decision == SignalDecision.TAKEN.value,
            SignalRow.dry_run.is_(False),
        )
        return list((await self._session.execute(statement)).scalars())

    async def published_since(self, since: datetime) -> int:
        """Signals created since an instant — specs/TELEGRAM_UX.md §6's daily cap.

        Dry-run signals count. The cap exists to stop the system talking too much,
        and a rehearsal day that ignored it would not rehearse the guard.
        """
        statement = select(func.count()).select_from(SignalRow).where(SignalRow.created_at >= since)
        return int((await self._session.execute(statement)).scalar_one())

    async def resolutions_since(self, since: datetime) -> list[tuple[str, datetime]]:
        """``(symbol, closed_at)`` for signals that resolved since an instant.

        Feeds ``rails.cooldown_until``. Only outcomes that *arm* a cooldown are
        returned: a stop-out, an expiry and an invalidation. A signal that reached
        its targets is not a reason to stay away from the symbol.
        """
        armed = (
            SignalStatus.STOPPED.value,
            SignalStatus.EXPIRED.value,
            SignalStatus.INVALIDATED.value,
        )
        statement = select(SignalRow.symbol, SignalRow.closed_at).where(
            SignalRow.status.in_(armed),
            SignalRow.closed_at.is_not(None),
            SignalRow.closed_at >= since,
        )
        rows = (await self._session.execute(statement)).all()
        return [(row.symbol, row.closed_at) for row in rows if row.closed_at is not None]

    async def realized_eur_since(self, since: datetime) -> list[Decimal]:
        """Signed realized P&L for taken signals closed since an instant (§7).

        Dry-run excluded: a paper loss cannot pause a real account.
        """
        statement = select(SignalRow.realized_eur).where(
            SignalRow.decision == SignalDecision.TAKEN.value,
            SignalRow.dry_run.is_(False),
            SignalRow.closed_at.is_not(None),
            SignalRow.closed_at >= since,
            SignalRow.realized_eur.is_not(None),
        )
        return [
            value
            for value in (await self._session.execute(statement)).scalars()
            if value is not None
        ]

    async def resolved_since(self, since: datetime | None = None) -> list[SignalRow]:
        """Signals with a measured outcome — the /stats population."""
        statement = select(SignalRow).where(SignalRow.closed_at.is_not(None))
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

    async def unposted(self, chat_id: int, *, limit: int = 100) -> list[SignalEventRow]:
        """Events with no delivered-or-claimed message for this chat, oldest first.

        A LEFT JOIN rather than a status column on the event: the event is a fact
        about the market and the message is a fact about Telegram, and a chat added
        later should receive the events it missed without the event row changing.
        """
        posted = select(TelegramMessageRow.signal_id, TelegramMessageRow.event_key).where(
            TelegramMessageRow.chat_id == chat_id,
            TelegramMessageRow.kind == MessageKind.UPDATE.value,
        )
        statement = (
            select(SignalEventRow)
            .where(
                tuple_(SignalEventRow.signal_id, SignalEventRow.event_key).not_in(posted),
            )
            .order_by(SignalEventRow.at, SignalEventRow.id)
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars())
