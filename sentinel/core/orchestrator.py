"""The 15-minute scan cycle — ARCHITECTURE.md §3's six steps, wired.

Everything the earlier milestones built exists; nothing ran it. This is the loop:
ingestion and features (M1/M2) → screener (M5) → charts (M3) → analyst (M5) →
risk gate (M4) → Telegram card (M6), with the rails, the audit trail and the
guards around it.

Four things it is careful about, each because getting them wrong costs money or
truth rather than merely being untidy:

**Per-symbol isolation** (PRD F1). A symbol whose snapshot will not assemble is
skipped and recorded in ``ingestion_failures``; it never blocks the rest of the
watchlist. The cycle is written to ``cycles`` before it starts and closed after,
so a cycle that dies leaves ``RUNNING`` behind as evidence rather than vanishing.

**Guards before spend, not after.** Dedup, cooldown, the daily signal cap and the
LLM spend guard all run *before* the deep analyst, because an analyst call is
~$0.32 and a symbol that cannot produce a signal should not cost one. The risk
gate re-checks the same rails afterwards — it is the authority, and this is an
optimisation that must never be the only check.

**The gate stays the gate.** Nothing here decides whether a plan is good. The
orchestrator assembles inputs, calls ``RiskEngine.evaluate`` and publishes what it
approves.

**Dry run publishes nothing.** The same cycle runs, the same card is rendered by
the same renderer and logged verbatim, and the signal is stored with
``dry_run=True`` so the tracker resolves it silently. A rehearsal that skipped the
analyst call would rehearse nothing worth knowing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sentinel.analyst.history import build_history_block
from sentinel.analyst.models import AnalystReport, CandidateStatus
from sentinel.analyst.providers.anthropic_fable import AnthropicFableAnalyst
from sentinel.bot.cards import signal_card
from sentinel.bot.formatting import zone_info
from sentinel.bot.models import SignalRecord
from sentinel.bot.publisher import SignalPublisher
from sentinel.bot.runtime import account_state, effective_config
from sentinel.charts.models import ChartImage, ChartSpec
from sentinel.charts.renderer import render_album
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import Settings
from sentinel.core.logging import get_logger
from sentinel.core.wiring import assemble_with_features, snapshot_assembler
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import FxRate, MarketSnapshot
from sentinel.llm.client import AnthropicClient
from sentinel.llm.errors import AnalystUnavailable
from sentinel.llm.models import LLMCall
from sentinel.llm.spend import SpendState, evaluate_spend, spend_window
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import (
    AccountState,
    GateStatus,
    MarketContext,
    PortfolioState,
    TradePlan,
)
from sentinel.risk.rails import cooldown_until, open_risk_pct
from sentinel.screener.screener import Screener
from sentinel.stats.queries import setup_stats
from sentinel.storage.db import Database
from sentinel.storage.repositories import (
    AnalystReportRepository,
    CycleRepository,
    FxRateRepository,
    GateDecisionRepository,
    LLMCallRepository,
    RiskStateRepository,
    RuntimeSettingsRepository,
    SignalRepository,
    SnapshotRepository,
)

log = get_logger(__name__)


@dataclass(frozen=True)
class CycleRepositories:
    """Which repository classes a cycle instantiates. The seam the bot and the
    tracker both carry, for the same reason: the guards below decide whether real
    money is analysed and published, and they must be testable on every run."""

    signals: type[SignalRepository] = SignalRepository
    cycles: type[CycleRepository] = CycleRepository
    gate_decisions: type[GateDecisionRepository] = GateDecisionRepository
    reports: type[AnalystReportRepository] = AnalystReportRepository
    llm_calls: type[LLMCallRepository] = LLMCallRepository
    snapshots: type[SnapshotRepository] = SnapshotRepository
    risk_state: type[RiskStateRepository] = RiskStateRepository
    settings: type[RuntimeSettingsRepository] = RuntimeSettingsRepository
    fx: type[FxRateRepository] = FxRateRepository


def select_symbols(
    interesting: set[str],
    *,
    open_symbols: set[str],
    cooldowns: dict[str, datetime],
    published_today: int,
    max_per_day: int,
    now: datetime,
) -> tuple[set[str], dict[str, str]]:
    """Which candidates may reach the deep analyst, and why the rest may not.

    ARCHITECTURE §3 step 6's dedup guard, PRD F11's "max 1 active signal per
    symbol", and specs/TELEGRAM_UX.md §6's daily cap — applied **before** the
    expensive tier, because an analyst call is ~$0.32 and a symbol that cannot
    produce a signal should not cost one. The risk gate re-checks all of it
    afterwards; this is an optimisation and never the only check.

    Pure, and returning the reasons rather than only the survivors: a cycle that
    analysed nothing has to be explicable from its own row, not from the logs.
    """
    allowed: set[str] = set()
    skipped: dict[str, str] = {}
    for symbol in sorted(interesting):
        cooldown = cooldowns.get(symbol)
        if symbol in open_symbols:
            skipped[symbol] = "a signal is already open for this symbol (PRD F11)"
        elif cooldown is not None and now < cooldown:
            skipped[symbol] = f"on cooldown until {cooldown.isoformat()}"
        elif published_today + len(allowed) >= max_per_day:
            skipped[symbol] = f"daily signal cap reached ({max_per_day})"
        else:
            allowed.add(symbol)
    return allowed, skipped


@dataclass
class CycleResult:
    """What one cycle did. Logged, stored on the ``cycles`` row, asserted in tests."""

    cycle_id: UUID
    dry_run: bool = False
    symbols_requested: int = 0
    symbols_scanned: int = 0
    symbols_skipped: int = 0
    candidates: int = 0
    analyzed: int = 0
    approved: int = 0
    published: int = 0
    spend_usd_estimate: Decimal = Decimal("0")
    analysis_suspended: bool = False
    suspended_reason: str | None = None
    #: The spend guard's verdict before this cycle spent anything and after it
    #: recorded what it spent. M8's admin alert messages on the *transition*
    #: between them, which is why the guard needs no "already warned today" row:
    #: exactly one cycle sees OK→WARN, and it is the cycle that caused it.
    spend_state_before: SpendState | None = None
    spend_state_after: SpendState | None = None
    #: Why each symbol was dropped before the analyst — a quiet cycle explains
    #: itself from the row rather than only from the logs.
    skipped: dict[str, str] = field(default_factory=dict)
    error: str | None = None


class CycleOrchestrator:
    """Runs one scan cycle."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        publisher: SignalPublisher | None = None,
        clock: Clock | None = None,
        repositories: CycleRepositories | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._publisher = publisher
        self._clock = clock or SystemClock()
        self._repos = repositories or CycleRepositories()

    async def run(self) -> CycleResult:
        cycle_id = uuid4()
        started = self._clock.now()

        stored = await self._runtime_state()
        config = effective_config(self._settings, stored)
        symbols = list(config.watchlist)
        result = CycleResult(
            cycle_id=cycle_id, dry_run=config.dry_run, symbols_requested=len(symbols)
        )

        async with self._database.session() as session:
            await self._repos.cycles(session).start(
                cycle_id, at=started, dry_run=config.dry_run, symbols=len(symbols)
            )
            await session.commit()

        try:
            await self._run_cycle(result, symbols=symbols, stored=stored, started=started)
            status = "OK"
        except Exception as exc:
            result.error = str(exc)[:512]
            status = "FAILED"
            log.error(
                "cycle.failed",
                cycle_id=str(cycle_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
        finally:
            await self._close_cycle(result, status=status)

        log.info(
            "cycle.complete",
            cycle_id=str(cycle_id),
            status=status,
            dry_run=result.dry_run,
            scanned=result.symbols_scanned,
            skipped=result.symbols_skipped,
            candidates=result.candidates,
            analyzed=result.analyzed,
            approved=result.approved,
            published=result.published,
            spend_usd=str(result.spend_usd_estimate),
        )
        return result

    # ---- the six steps -----------------------------------------------------

    async def _run_cycle(
        self,
        result: CycleResult,
        *,
        symbols: list[str],
        stored: dict[str, object],
        started: datetime,
    ) -> None:
        config = effective_config(self._settings, stored)
        key = self._settings.secrets.anthropic_api_key
        if key is None:
            result.suspended_reason = "no ANTHROPIC_API_KEY"
            result.analysis_suspended = True
            log.warning("cycle.no_api_key", detail="the pipeline cannot analyse without a key")
            return

        # 1. Snapshots + features, per-symbol isolated inside the assembler.
        async with snapshot_assembler(
            self._settings, last_known_good_fx=self._last_known_good_fx
        ) as assembler:
            snapshots, features = await assemble_with_features(
                assembler, symbols, config.features, cycle_id=result.cycle_id
            )
            result.symbols_scanned = len(snapshots)
            result.symbols_skipped = len(symbols) - len(snapshots)

            async with self._database.session() as session:
                repo = self._repos.snapshots(session)
                for snapshot in snapshots:
                    await repo.save(snapshot)
                await session.commit()

            if not snapshots:
                log.warning("cycle.no_snapshots", cycle_id=str(result.cycle_id))
                return

            client = AnthropicClient(config.llm, api_key=key.get_secret_value())
            try:
                await self._analyse(
                    result,
                    snapshots=snapshots,
                    features=features,
                    stored=stored,
                    client=client,
                    started=started,
                )
            finally:
                await client.aclose()

    async def _analyse(
        self,
        result: CycleResult,
        *,
        snapshots: list[MarketSnapshot],
        features: dict[str, SymbolFeatures],
        stored: dict[str, object],
        client: AnthropicClient,
        started: datetime,
    ) -> None:
        config = effective_config(self._settings, stored)
        calls: list[LLMCall] = []

        # 2. Screener over everything that survived ingestion.
        screener = Screener(client, config, cycle_id=result.cycle_id)
        verdicts = await screener.screen(snapshots, features)
        calls.extend(verdicts.calls)
        interesting = {verdict.symbol for verdict in verdicts.interesting}
        result.candidates = len(interesting)

        # 3. Guards, before the expensive tier. Each drop is recorded with a
        #    reason so a silent cycle is explicable.
        allowed = await self._allowed(interesting, result, started=started, config=config)

        # Read once, before anything expensive runs. The screener's own calls are
        # not recorded yet, so this is genuinely "where the day stood when this
        # cycle began" — the left-hand side of the transition M8 alerts on.
        state, reason = await self._spend_state(started)
        result.spend_state_before = state

        # A pause holds back the expensive tier too. The gate would reject every
        # plan with PAUSED anyway (§2 rule 7), so analysing first would buy a
        # ~$0.32 rejection; the screener still runs, because its verdicts are the
        # audit trail of what the market was doing while the system was stopped.
        if allowed and await self._paused():
            result.analysis_suspended = True
            result.suspended_reason = "paused"
            for symbol in allowed:
                result.skipped[symbol] = "paused"
            log.info(
                "cycle.paused",
                cycle_id=str(result.cycle_id),
                held_back=sorted(allowed),
                detail="the screener and the tracker keep running",
            )
            allowed = set()

        # The spend guard bites here, after the cheap pass and before the
        # expensive one — exactly the split it is meant to make.
        if allowed and state is SpendState.LIMIT_REACHED:
            result.analysis_suspended = True
            result.suspended_reason = reason
            for symbol in allowed:
                result.skipped[symbol] = "spend limit reached"
            log.warning(
                "cycle.analysis_suspended",
                cycle_id=str(result.cycle_id),
                reason=reason,
                held_back=sorted(allowed),
                detail="the screener and the tracker keep running",
            )
            allowed = set()

        # 4-5. Deep analysis, gate, publish.
        for snapshot in snapshots:
            if snapshot.symbol not in allowed:
                continue
            calls.extend(
                await self._analyse_symbol(
                    result, snapshot=snapshot, features=features, stored=stored, client=client
                )
            )

        result.spend_usd_estimate = sum((call.cost_usd_estimate for call in calls), Decimal(0))
        async with self._database.session() as session:
            await self._repos.llm_calls(session).record_many(calls)
            await session.commit()

        # Re-read *after* the commit above, so this cycle's own calls are in the
        # total. A guard that only ever looked at yesterday's spend would notice
        # the crossing one cycle late — fifteen minutes and ~$0.32 too late.
        result.spend_state_after, _ = await self._spend_state(started)

    async def _analyse_symbol(
        self,
        result: CycleResult,
        *,
        snapshot: MarketSnapshot,
        features: dict[str, SymbolFeatures],
        stored: dict[str, object],
        client: AnthropicClient,
    ) -> list[LLMCall]:
        config = effective_config(self._settings, stored)
        charts = list(
            render_album(snapshot, features[snapshot.symbol], self._chart_specs(snapshot.symbol))
        )
        history = await self._history_block(snapshot.symbol)

        analyst = AnthropicFableAnalyst(client, config, cycle_id=result.cycle_id)
        try:
            report = await analyst.analyze(snapshot, charts, history)
        except AnalystUnavailable as exc:
            log.warning(
                "cycle.analyst_unavailable",
                symbol=snapshot.symbol,
                reason=exc.reason,
                detail=exc.detail,
            )
            return list(analyst.calls)

        result.analyzed += 1
        async with self._database.session() as session:
            await self._repos.reports(session).save(
                report,
                created_at=snapshot.captured_at,
                provider=AnthropicFableAnalyst.name,
                cycle_id=result.cycle_id,
                snapshot_id=snapshot.snapshot_id,
            )
            await session.commit()

        if report.candidate_status is not CandidateStatus.CANDIDATE:
            log.info(
                "cycle.not_a_candidate",
                symbol=snapshot.symbol,
                status=report.candidate_status.value,
            )
            return list(analyst.calls)

        await self._gate_and_publish(
            result, report=report, snapshot=snapshot, stored=stored, charts=charts
        )
        return list(analyst.calls)

    async def _gate_and_publish(
        self,
        result: CycleResult,
        *,
        report: AnalystReport,
        snapshot: MarketSnapshot,
        stored: dict[str, object],
        charts: list[ChartImage],
    ) -> None:
        config = effective_config(self._settings, stored)
        account, portfolio = await self._gate_inputs(stored, symbol=snapshot.symbol)

        features_model = (
            SymbolFeatures.model_validate(snapshot.features) if snapshot.features else None
        )
        decision = RiskEngine(config, clock=self._clock).evaluate(
            report=report,
            market=MarketContext.from_snapshot(snapshot, features_model),
            account=account,
            portfolio=portfolio,
        )

        async with self._database.session() as session:
            await self._repos.gate_decisions(session).record(decision, cycle_id=result.cycle_id)
            await session.commit()

        if decision.status is not GateStatus.APPROVED_FOR_HUMAN or decision.plan is None:
            log.info(
                "cycle.not_approved",
                symbol=snapshot.symbol,
                status=decision.status.value,
                reason=None if decision.reason is None else decision.reason.value,
            )
            return

        result.approved += 1
        if config.dry_run:
            await self._record_dry_run(result, decision.plan, charts)
            return

        if self._publisher is None:
            log.warning(
                "cycle.no_publisher",
                symbol=snapshot.symbol,
                detail="plan approved but no Telegram bot is configured — it is stored, not sent",
            )
            return

        published = await self._publisher.publish(
            decision.plan, tuple(charts), cycle_id=result.cycle_id
        )
        if published.published:
            result.published += 1

    async def _record_dry_run(
        self, result: CycleResult, plan: TradePlan, charts: list[ChartImage]
    ) -> None:
        """Store the signal and log the card that would have been sent.

        Rendered by the *same* function the publisher calls, so what the log holds
        is what the owner would have read — not a summary of it. Nothing reaches
        Telegram, and the tracker picks the signal up on its next tick.
        """
        record = SignalRecord(
            plan=plan,
            cycle_id=result.cycle_id,
            chart_params=tuple(chart.params.to_json_dict() for chart in charts),
            dry_run=True,
        )
        async with self._database.session() as session:
            claimed = await self._repos.signals(session).claim(record)
            await session.commit()
        if claimed is None:  # pragma: no cover — a fresh plan_id per evaluation
            return

        result.published += 1
        tz = zone_info(self._settings.config.telegram.owner_timezone)
        log.info(
            "cycle.dry_run_card",
            cycle_id=str(result.cycle_id),
            symbol=plan.symbol,
            number=claimed.number,
            detail="not sent — dry_run is on",
            card=signal_card(claimed, tz),
        )

    # ---- guards ------------------------------------------------------------

    async def _allowed(
        self,
        interesting: set[str],
        result: CycleResult,
        *,
        started: datetime,
        config: object,
    ) -> set[str]:
        """Dedup, cooldown and the daily cap — ARCHITECTURE §3 step 6 and §6.

        Run before the analyst so a symbol that cannot produce a signal does not
        cost one. The risk gate checks the same rails again on the way out; this
        is an optimisation, never the only check.
        """
        risk = self._settings.config.risk
        day_start = started.replace(hour=0, minute=0, second=0, microsecond=0)

        async with self._database.session() as session:
            signals = self._repos.signals(session)
            open_symbols = await signals.open_symbols()
            resolutions = await signals.resolutions_since(
                started - timedelta(hours=risk.signal_cooldown_hours)
            )
            today = await signals.published_since(day_start)

        allowed, skipped = select_symbols(
            interesting,
            open_symbols=open_symbols,
            cooldowns=cooldown_until(resolutions, hours=risk.signal_cooldown_hours),
            published_today=today,
            max_per_day=risk.max_signals_per_day,
            now=started,
        )
        result.skipped.update(skipped)

        for symbol, reason in result.skipped.items():
            log.info("cycle.symbol_skipped", symbol=symbol, reason=reason)
        return allowed

    async def _paused(self) -> bool:
        async with self._database.session() as session:
            pause = await self._repos.risk_state(session).load()
        return pause.is_active(self._clock.now())

    async def _spend_state(self, started: datetime) -> tuple[SpendState, str]:
        day_start, month_start = spend_window(started)
        async with self._database.session() as session:
            totals = await self._repos.llm_calls(session).spend_totals(
                day_start=day_start,
                month_start=month_start,
                priced_models=tuple(self._settings.config.llm.pricing),
            )
        state = evaluate_spend(totals, self._settings.config.llm)
        floor = "at least " if totals.is_floor else ""
        return state, (
            f"{floor}${totals.day_usd} spent today against a "
            f"${self._settings.config.llm.daily_spend_limit_usd} limit"
        )

    # ---- inputs ------------------------------------------------------------

    async def _runtime_state(self) -> dict[str, object]:
        async with self._database.session() as session:
            return await self._repos.settings(session).all()

    async def _gate_inputs(
        self, stored: dict[str, object], *, symbol: str
    ) -> tuple[AccountState, PortfolioState]:
        """Everything §2 rule 7 reads, built from the database (M4's note to M7)."""
        config = effective_config(self._settings, stored)
        risk = config.risk
        now = self._clock.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        async with self._database.session() as session:
            fx = await self._repos.fx(session).get()
            pause = await self._repos.risk_state(session).load()
            signals = self._repos.signals(session)
            open_taken = await signals.open_taken()
            resolutions = await signals.resolutions_since(
                now - timedelta(hours=risk.signal_cooldown_hours)
            )
            today = await signals.published_since(day_start)

        account = account_state(stored, config, fx.rate if fx is not None else Decimal("1"))
        plans = [TradePlan.model_validate(row.plan) for row in open_taken]
        portfolio = PortfolioState(
            open_risk_pct=open_risk_pct([plan.risk_per_trade_pct for plan in plans]),
            open_positions=len(plans),
            cooldown_until=cooldown_until(resolutions, hours=risk.signal_cooldown_hours),
            pause=pause,
            signals_today=today,
        )
        return account, portfolio

    async def _history_block(self, symbol: str) -> str:
        """specs/PROMPTS.md §3 — and from M7 both halves are real."""
        async with self._database.session() as session:
            verdicts = await self._repos.reports(session).recent_for_symbol(
                symbol, limit=self._settings.config.llm.history_verdicts
            )
            stats = await setup_stats(session, now=self._clock.now())
        return build_history_block(symbol, verdicts, stats)

    async def _last_known_good_fx(self) -> FxRate | None:
        async with self._database.session() as session:
            return await self._repos.fx(session).get()

    def _chart_specs(self, symbol: str) -> tuple[ChartSpec, ...]:
        charts = self._settings.config.charts
        return tuple(
            ChartSpec(
                symbol=symbol,
                timeframe=timeframe,
                candle_window=charts.candle_window,
                width_px=charts.width_px,
                height_px=charts.height_px,
                dpi=charts.dpi,
                volume_panel_ratio=charts.volume_panel_ratio,
                ema_periods=charts.ema_periods,
                max_levels=charts.max_levels,
            )
            for timeframe in charts.timeframes
        )

    async def _close_cycle(self, result: CycleResult, *, status: str) -> None:
        async with self._database.session() as session:
            await self._repos.cycles(session).finish(
                result.cycle_id,
                at=self._clock.now(),
                status=status,
                symbols_scanned=result.symbols_scanned,
                symbols_skipped=result.symbols_skipped + len(result.skipped),
                candidates=result.candidates,
                analyzed=result.analyzed,
                approved=result.approved,
                published=result.published,
                spend_usd_estimate=result.spend_usd_estimate,
                analysis_suspended=result.analysis_suspended,
                suspended_reason=result.suspended_reason,
                error=result.error,
            )
            await session.commit()


__all__ = ["CycleOrchestrator", "CycleRepositories", "CycleResult", "select_symbols"]
