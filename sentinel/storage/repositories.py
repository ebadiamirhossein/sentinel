"""Repositories — the only place that turns domain models into rows.

Serialization is split out into pure functions (`snapshot_context`,
`snapshot_sources`, `candle_rows`) so it can be tested without a database.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.analyst.history import PastVerdict
from sentinel.analyst.models import AnalystReport, CandidateStatus, SetupType
from sentinel.bot.models import (
    MessageKind,
    MessageStatus,
    PostedMessage,
    SignalDecision,
    SignalRecord,
)
from sentinel.ingestion.models import FxRate, InstrumentMeta, MarketSnapshot, Stamped
from sentinel.llm.models import LLMCall
from sentinel.risk.models import GateDecision, PauseReason, PauseState, TradePlan
from sentinel.storage.models import (
    AnalystReportRow,
    ConfigChangeRow,
    FxRateRow,
    GateDecisionRow,
    IngestionFailureRow,
    InstrumentMetaRow,
    LLMCallRow,
    MarketSnapshotRow,
    OhlcvCandleRow,
    RiskStateRow,
    RuntimeSettingRow,
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

        ``outcome`` stays ``None``: nothing measures outcomes until M7's tracker,
        and the history block says so in words rather than implying a result.
        """
        result = await self._session.execute(
            select(AnalystReportRow)
            .where(AnalystReportRow.symbol == symbol, AnalystReportRow.role == role)
            .order_by(AnalystReportRow.created_at.desc())
            .limit(limit)
        )
        return [
            PastVerdict(
                created_at=row.created_at,
                candidate_status=CandidateStatus(row.candidate_status),
                setup_type=SetupType(row.setup_type),
                direction=row.direction,
                confidence=row.confidence,
                thesis=row.thesis,
                prompt_version=row.prompt_version,
                outcome=None,
            )
            for row in result.scalars().all()
        ]


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


class TelegramMessageRepository:
    """specs/TELEGRAM_UX.md §6 — message ids stored, restarts never double-post.

    Claim-then-send, in two steps, because there is no third option that is safe:
    claiming after the send loses the id if the process dies in between, and not
    claiming at all means every restart re-posts.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self, signal_id: UUID, kind: MessageKind, chat_id: int, *, at: datetime
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
                status=MessageStatus.PENDING.value,
                claimed_at=at,
            )
            .on_conflict_do_nothing(index_elements=["signal_id", "kind", "chat_id"])
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
    ) -> None:
        row = await self._row(signal_id, kind, chat_id)
        if row is None:  # pragma: no cover — confirm always follows a successful claim
            return
        row.message_id = message_id
        row.status = MessageStatus.SENT.value
        row.sent_at = at
        row.error = None

    async def fail(self, signal_id: UUID, kind: MessageKind, chat_id: int, *, error: str) -> None:
        row = await self._row(signal_id, kind, chat_id)
        if row is None:  # pragma: no cover — fail always follows a successful claim
            return
        row.status = MessageStatus.FAILED.value
        row.error = error[:512]

    async def get(self, signal_id: UUID, kind: MessageKind, chat_id: int) -> PostedMessage | None:
        row = await self._row(signal_id, kind, chat_id)
        if row is None:
            return None
        return PostedMessage(
            signal_id=row.signal_id,
            kind=MessageKind(row.kind),
            chat_id=row.chat_id,
            message_id=row.message_id,
            status=MessageStatus(row.status),
            error=row.error,
        )

    async def stuck(self) -> list[TelegramMessageRow]:
        """Claims that never reached ``SENT`` — surfaced by ``/status``."""
        statement = select(TelegramMessageRow).where(
            TelegramMessageRow.status != MessageStatus.SENT.value
        )
        return list((await self._session.execute(statement)).scalars())

    async def _row(
        self, signal_id: UUID, kind: MessageKind, chat_id: int
    ) -> TelegramMessageRow | None:
        statement = select(TelegramMessageRow).where(
            TelegramMessageRow.signal_id == signal_id,
            TelegramMessageRow.kind == kind.value,
            TelegramMessageRow.chat_id == chat_id,
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
