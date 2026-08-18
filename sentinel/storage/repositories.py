"""Repositories — the only place that turns domain models into rows.

Serialization is split out into pure functions (`snapshot_context`,
`snapshot_sources`, `candle_rows`) so it can be tested without a database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.ingestion.models import FxRate, InstrumentMeta, MarketSnapshot, Stamped
from sentinel.risk.models import GateDecision, PauseReason, PauseState
from sentinel.storage.models import (
    FxRateRow,
    GateDecisionRow,
    IngestionFailureRow,
    InstrumentMetaRow,
    MarketSnapshotRow,
    OhlcvCandleRow,
    RiskStateRow,
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
