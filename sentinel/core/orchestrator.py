"""The scan cycle — ARCHITECTURE.md §3's six steps, wired.

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

**One analysis per cycle, shared; sizing per user (M8.1).** Steps 1-4 — snapshot,
features, screener, charts, deep analyst, and the ``analyst_reports`` row — happen
**exactly once per symbol per cycle** no matter how many people are approved. The
analyst produces a judgment about a market, not about a person, and it costs ~$0.32
a call. Only step 5 fans out: for each eligible user, their own ``AccountState`` and
``PortfolioState``, their own ``RiskEngine.evaluate``, their own ``gate_decisions``
row, their own signal and their own card. ``sentinel/risk/`` is unchanged — it was
already a pure function of (account, portfolio), so multi-user is a matter of what is
handed to it.

The pre-analyst guard runs on the **union** of eligible users: a symbol is analysed
if *any* of them could receive a signal for it, because one person's cooldown must
not suppress a shared analysis for everybody. ARCHITECTURE §3 already says the early
check is an optimisation and never the only one; the per-user check runs again inside
the fan-out, where it is authoritative.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sentinel.analyst.history import build_history_block
from sentinel.analyst.models import AnalystReport, CandidateStatus
from sentinel.analyst.providers.anthropic_fable import AnthropicFableAnalyst
from sentinel.bot.cards import no_capital_card, signal_card
from sentinel.bot.formatting import zone_info
from sentinel.bot.models import SignalRecord, UserAccount
from sentinel.bot.notices import UserNotifier, no_capital_key
from sentinel.bot.publisher import SignalPublisher
from sentinel.bot.runtime import account_state, effective_config
from sentinel.charts.models import ChartImage, ChartSpec
from sentinel.charts.renderer import render_album
from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import AppConfig, Settings
from sentinel.core.logging import get_logger
from sentinel.core.wiring import assemble_with_features, snapshot_assembler
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import FxRate, MarketSnapshot
from sentinel.llm.client import AnthropicClient
from sentinel.llm.errors import AnalystUnavailable
from sentinel.llm.models import LLMCall, LLMCallStatus
from sentinel.llm.spend import SpendState, evaluate_spend, spend_window
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import (
    AccountState,
    GateStatus,
    MarketContext,
    PortfolioState,
    RejectionReason,
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
    UserRepository,
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
    users: type[UserRepository] = UserRepository


class SkipReason(StrEnum):
    """Why a screened symbol never reached the deep analyst (M8.2).

    A **fixed vocabulary**, stored on ``cycles.skipped`` rather than only logged.
    M9 has to answer "what is the system declining to analyse, and why" in SQL:
    logs rotate, a column does not, and free text cannot be grouped. The prose that
    accompanied each of these through M7/M8.1 survives as ``Skip.detail`` — a
    cooldown is more useful with its expiry attached — but the *key* is closed.
    """

    #: PRD F11 — this user already holds an open signal on the symbol.
    OPEN_SIGNAL = "OPEN_SIGNAL"
    #: ARCHITECTURE §3.6 — resolved too recently to re-signal.
    COOLDOWN = "COOLDOWN"
    #: specs/TELEGRAM_UX.md §6's anti-spam cap.
    DAILY_CAP = "DAILY_CAP"
    #: M8.1 — nobody approved has capital set, so no plan could be sized.
    NO_FUNDED_USER = "NO_FUNDED_USER"
    #: The operator's system-wide /pause.
    PAUSED = "PAUSED"
    #: The daily LLM spend limit was already reached when the cycle began.
    SPEND_LIMIT = "SPEND_LIMIT"
    #: M8.2 — the last deep analysis of this symbol said WATCHLIST or NO_SETUP and
    #: the setup timeframe has not produced a new candle since. Re-asking costs
    #: ~$0.28 to be told the same thing about the same unclosed bar.
    RECENTLY_ANALYSED = "RECENTLY_ANALYSED"


@dataclass(frozen=True)
class Skip:
    """One dropped symbol: a countable reason and the sentence a human wants."""

    reason: SkipReason
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"reason": self.reason.value, "detail": self.detail}


def select_symbols(
    interesting: set[str],
    *,
    open_symbols: set[str],
    cooldowns: dict[str, datetime],
    published_today: int,
    max_per_day: int,
    now: datetime,
    recently_analysed: dict[str, datetime] | None = None,
) -> tuple[set[str], dict[str, Skip]]:
    """Which candidates may reach the deep analyst, and why the rest may not.

    ARCHITECTURE §3 step 6's dedup guard, PRD F11's "max 1 active signal per
    symbol", and specs/TELEGRAM_UX.md §6's daily cap — applied **before** the
    expensive tier, because an analyst call is ~$0.32 and a symbol that cannot
    produce a signal should not cost one. The risk gate re-checks the cooldown and
    the cap afterwards; it has no dedup rail of its own, which is why this function
    is also applied **per user** inside the fan-out (M8.1) rather than only over the
    union. Without that second application, a symbol another user was owed would
    hand this user a second signal on a position they already hold.

    Pure, and returning the reasons rather than only the survivors: a cycle that
    analysed nothing has to be explicable from its own row, not from the logs.
    """
    allowed: set[str] = set()
    skipped: dict[str, Skip] = {}
    fresh = recently_analysed or {}
    for symbol in sorted(interesting):
        cooldown = cooldowns.get(symbol)
        quiet_until = fresh.get(symbol)
        if symbol in open_symbols:
            skipped[symbol] = Skip(
                SkipReason.OPEN_SIGNAL, "a signal is already open for this symbol (PRD F11)"
            )
        elif cooldown is not None and now < cooldown:
            skipped[symbol] = Skip(SkipReason.COOLDOWN, f"on cooldown until {cooldown.isoformat()}")
        # Before the daily cap, deliberately: a symbol suppressed as already-analysed
        # was never a signal, so it must not consume one of the day's slots.
        elif quiet_until is not None and now < quiet_until:
            skipped[symbol] = Skip(
                SkipReason.RECENTLY_ANALYSED,
                f"analysed since {quiet_until.isoformat()} and not a candidate then",
            )
        elif published_today + len(allowed) >= max_per_day:
            skipped[symbol] = Skip(
                SkipReason.DAILY_CAP, f"daily signal cap reached ({max_per_day})"
            )
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
    #: itself from the row rather than only from the logs. Union-level from M8.1: a
    #: symbol appears here only when *no* eligible user could have received it.
    skipped: dict[str, Skip] = field(default_factory=dict)
    #: How many users the fan-out sized plans for (M8.1). Zero means the deep
    #: analyst was not called at all — nobody should pay $0.32 for a plan with no
    #: recipient. Not stored on ``cycles``: it is a property of the moment, and
    #: ``gate_decisions.user_id`` is the durable record of who was evaluated.
    recipients: int = 0
    error: str | None = None


class CycleOrchestrator:
    """Runs one scan cycle."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        publisher_factory: Callable[[int], SignalPublisher] | None = None,
        notices: UserNotifier | None = None,
        clock: Clock | None = None,
        repositories: CycleRepositories | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        #: One publisher per recipient (M8.1). A factory rather than an instance,
        #: because each user's card goes to their own chat and carries their own
        #: ``user_id`` onto the signal row. ``None`` means no bot is configured — the
        #: plan is still gated, stored and logged.
        self._publisher_factory = publisher_factory
        self._notices = notices
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

        # 2. Screener over everything that survived ingestion. Shared: one call for
        #    the whole watchlist, whoever is approved.
        screener = Screener(client, config, cycle_id=result.cycle_id)
        verdicts = await screener.screen(snapshots, features)
        calls.extend(verdicts.calls)
        interesting = {verdict.symbol for verdict in verdicts.interesting}
        result.candidates = len(interesting)

        # 3. Guards, before the expensive tier. Each drop is recorded with a
        #    reason so a silent cycle is explicable. Who could receive anything is
        #    part of that: with nobody set up, the deep analyst is not called at all.
        recipients = await self._recipients(now=started)
        result.recipients = len(recipients)
        allowed = await self._allowed(
            interesting, result, started=started, config=config, recipients=recipients
        )

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
                result.skipped[symbol] = Skip(SkipReason.PAUSED, "paused")
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
                result.skipped[symbol] = Skip(SkipReason.SPEND_LIMIT, "spend limit reached")
            log.warning(
                "cycle.analysis_suspended",
                cycle_id=str(result.cycle_id),
                reason=reason,
                held_back=sorted(allowed),
                detail="the screener and the tracker keep running",
            )
            allowed = set()

        # 4-5. Deep analysis once per symbol, then the gate and the card once per
        #      eligible user. The owner's book is what the history block calibrates on.
        owner_id = await self._owner_id()
        for snapshot in snapshots:
            if snapshot.symbol not in allowed:
                continue
            calls.extend(
                await self._analyse_symbol(
                    result,
                    snapshot=snapshot,
                    features=features,
                    stored=stored,
                    client=client,
                    recipients=recipients,
                    owner_id=owner_id,
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
        recipients: Sequence[UserAccount],
        owner_id: int | None,
    ) -> list[LLMCall]:
        config = effective_config(self._settings, stored)
        charts = list(
            render_album(snapshot, features[snapshot.symbol], self._chart_specs(snapshot.symbol))
        )
        history = await self._history_block(snapshot.symbol, owner_id=owner_id)

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
        # Which call produced this report (M8.2). `save()` has taken `llm_call_id`
        # since M5 and nothing ever passed it, so the column was NULL on every row —
        # leaving `(cycle_id, symbol)` as the only way to join a verdict to its cost.
        # That join is wrong exactly where it matters: a schema-invalid response is
        # retried, so a symbol can have two calls in one cycle and the retry's cost
        # would be attributed to the report the *other* call produced. The OK call is
        # the last one, because `analyze` returns as soon as one parses.
        ok_call = next(
            (call for call in reversed(analyst.calls) if call.status is LLMCallStatus.OK), None
        )
        async with self._database.session() as session:
            await self._repos.reports(session).save(
                report,
                created_at=snapshot.captured_at,
                provider=AnthropicFableAnalyst.name,
                cycle_id=result.cycle_id,
                snapshot_id=snapshot.snapshot_id,
                llm_call_id=None if ok_call is None else ok_call.call_id,
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
            result,
            report=report,
            snapshot=snapshot,
            stored=stored,
            charts=charts,
            recipients=recipients,
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
        recipients: Sequence[UserAccount],
    ) -> None:
        """Step 5, once per eligible user — the whole of the fan-out.

        The report and the charts above are shared and already paid for. Everything
        from here is personal: the capital, the risk %, the rails, the plan, the card
        and the row it is stored in.
        """
        for user in recipients:
            await self._gate_for(
                result,
                user=user,
                report=report,
                snapshot=snapshot,
                stored=stored,
                charts=charts,
            )

    async def _gate_for(
        self,
        result: CycleResult,
        *,
        user: UserAccount,
        report: AnalystReport,
        snapshot: MarketSnapshot,
        stored: dict[str, object],
        charts: list[ChartImage],
    ) -> None:
        config = effective_config(self._settings, stored)
        account, portfolio, dedup = await self._gate_inputs(
            user, stored=stored, symbol=snapshot.symbol
        )
        if dedup is not None:
            # The gate has no dedup rail of its own (§2 rule 7 covers cooldown, the
            # cap, positions and open risk, not "already holding this symbol"), so
            # the pure guard is applied again here per user. Recorded in the log and
            # not in ``gate_decisions``: nothing was evaluated, so there is no verdict.
            log.info(
                "cycle.user_skipped",
                symbol=snapshot.symbol,
                user_id=user.telegram_user_id,
                reason=dedup.reason.value,
                detail=dedup.detail,
            )
            return

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
            await self._repos.gate_decisions(session).record(
                decision, cycle_id=result.cycle_id, user_id=user.telegram_user_id
            )
            await session.commit()

        if decision.status is not GateStatus.APPROVED_FOR_HUMAN or decision.plan is None:
            log.info(
                "cycle.not_approved",
                symbol=snapshot.symbol,
                user_id=user.telegram_user_id,
                status=decision.status.value,
                reason=None if decision.reason is None else decision.reason.value,
            )
            if decision.reason is RejectionReason.NO_CAPITAL:
                await self._say_no_capital(user)
            return

        result.approved += 1
        if config.dry_run:
            await self._record_dry_run(result, decision.plan, charts, user_id=user.telegram_user_id)
            return

        if self._publisher_factory is None:
            log.warning(
                "cycle.no_publisher",
                symbol=snapshot.symbol,
                user_id=user.telegram_user_id,
                detail="plan approved but no Telegram bot is configured — it is stored, not sent",
            )
            return

        published = await self._publisher_factory(user.telegram_user_id).publish(
            decision.plan, tuple(charts), cycle_id=result.cycle_id
        )
        if published.published:
            result.published += 1

    async def _say_no_capital(self, user: UserAccount) -> None:
        """Tell an approved user why an approved plan did not reach them (M8.1).

        Fired from the gate's own ``NO_CAPITAL`` verdict rather than from a check up
        front, so the message is only ever sent on a day when it actually cost them
        something. Once per UTC day per user, claimed in ``telegram_messages``.
        """
        if self._notices is None:
            return
        await self._notices.notice(
            user.telegram_user_id,
            key=no_capital_key(user.telegram_user_id, self._clock.now()),
            text=no_capital_card(),
        )

    async def _record_dry_run(
        self, result: CycleResult, plan: TradePlan, charts: list[ChartImage], *, user_id: int
    ) -> None:
        """Store the signal and log the card that would have been sent.

        Rendered by the *same* function the publisher calls, so what the log holds
        is what the owner would have read — not a summary of it. Nothing reaches
        Telegram, and the tracker picks the signal up on its next tick.

        Inside the fan-out from M8.1, so a rehearsal rehearses the fan-out: each
        user gets their own row, sized against their own capital, and the logged card
        is theirs.
        """
        record = SignalRecord(
            plan=plan,
            user_id=user_id,
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
            user_id=user_id,
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
        config: AppConfig,
        recipients: Sequence[UserAccount],
    ) -> set[str]:
        """Dedup, cooldown and the daily cap — ARCHITECTURE §3 step 6 and §6.

        Run before the analyst so a symbol that cannot produce a signal does not
        cost one. The risk gate checks the same rails again on the way out; this
        is an optimisation, never the only check.

        **Over the union of eligible users (M8.1).** A symbol survives if it survives
        for at least one of them, because the analysis is shared: letting one member's
        four-hour cooldown suppress a BTC analysis for everybody would make the
        cheapest guard in the system the most expensive mistake. The reason recorded
        against a dropped symbol is the *first* user's — they agree in the common case
        of one user, and where they disagree the symbol was not dropped at all.

        Only users with capital set count towards the union: a plan that cannot be
        sized is not a reason to spend $0.32 (they are still evaluated in the
        fan-out, and told why nothing arrived).
        """
        risk = config.risk
        day_start = started.replace(hour=0, minute=0, second=0, microsecond=0)
        funded = [user for user in recipients if user.capital_set]
        if not funded:
            for symbol in sorted(interesting):
                result.skipped[symbol] = Skip(
                    SkipReason.NO_FUNDED_USER, "no user is set up to receive a signal"
                )
            self._log_skips(result)
            return set()

        async with self._database.session() as session:
            signals = self._repos.signals(session)
            open_by_user = await signals.open_symbols_by_user()
            resolutions_by_user = await signals.resolutions_by_user_since(
                started - timedelta(hours=risk.signal_cooldown_hours)
            )
            today_by_user = await signals.published_by_user_since(day_start)

        # M8.2's re-analysis cooldown is deliberately **not** per user. The other
        # rails ask "may this person receive a signal"; this one asks "did we already
        # pay to have this symbol looked at". The analysis is shared, so the answer
        # is too — making it per user would let a second member's fresh slate buy the
        # same $0.28 verdict the owner was just given.
        quiet_until = await self._recently_analysed(started, config=config)

        allowed: set[str] = set()
        reasons: dict[str, Skip] = {}
        for user in funded:
            uid = user.telegram_user_id
            mine, skipped = select_symbols(
                interesting,
                open_symbols=open_by_user.get(uid, set()),
                cooldowns=cooldown_until(
                    resolutions_by_user.get(uid, []), hours=risk.signal_cooldown_hours
                ),
                published_today=today_by_user.get(uid, 0),
                max_per_day=risk.max_signals_per_day,
                now=started,
                recently_analysed=quiet_until,
            )
            allowed |= mine
            for symbol, skip in skipped.items():
                reasons.setdefault(symbol, skip)

        result.skipped.update({s: r for s, r in reasons.items() if s not in allowed})
        self._log_skips(result)
        return allowed

    async def _recently_analysed(
        self, started: datetime, *, config: AppConfig
    ) -> dict[str, datetime]:
        """Symbols whose last deep analysis was not a candidate, and are still quiet.

        Returns ``{symbol: quiet_until}``. The window is one setup-timeframe candle
        (``schedule.reanalysis_cooldown_minutes``, 60): the analyst reads a 1h chart,
        so asking again before that bar closes is asking about the same bar.

        Only ``WATCHLIST`` and ``NO_SETUP`` count. A ``CANDIDATE`` that the *gate*
        then rejected is not suppressed — the analysis was right and the rails were
        what stopped it, and those rails have their own reasons above.
        """
        minutes = config.schedule.reanalysis_cooldown_minutes
        if minutes <= 0:
            return {}
        since = started - timedelta(minutes=minutes)
        async with self._database.session() as session:
            latest = await self._repos.reports(session).latest_non_candidates(since=since)
        return {symbol: at + timedelta(minutes=minutes) for symbol, at in latest.items()}

    @staticmethod
    def _log_skips(result: CycleResult) -> None:
        for symbol, skip in result.skipped.items():
            log.info(
                "cycle.symbol_skipped",
                symbol=symbol,
                reason=skip.reason.value,
                detail=skip.detail,
            )

    async def _recipients(self, *, now: datetime) -> list[UserAccount]:
        """Everyone a plan may be sized for this cycle, in a stable order.

        APPROVED, has accepted the current first-run note, and not held by their own
        daily-loss pause. Capital is *not* required here — a user who is set up but
        has no capital still goes through the gate so that the ``NO_CAPITAL`` verdict
        can tell them why nothing arrived (``_say_no_capital``).

        Ordered by Telegram id so a cycle's fan-out is reproducible, which matters
        when the daily cap or the open-risk budget is what decides who gets the last
        signal of the day.
        """
        async with self._database.session() as session:
            approved = await self._repos.users(session).approved()
        return [user for user in approved if user.eligible_for_signals(now)]

    async def _owner_id(self) -> int | None:
        """Whose book calibrates the analyst (specs/PROMPTS.md §3, M8.1)."""
        async with self._database.session() as session:
            owner = await self._repos.users(session).owner()
        if owner is not None:
            return owner.telegram_user_id
        return self._settings.secrets.owner_user_id

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
        self, user: UserAccount, *, stored: dict[str, object], symbol: str
    ) -> tuple[AccountState, PortfolioState, Skip | None]:
        """Everything §2 rule 7 reads for **one user**, built from the database.

        Re-read per (user, symbol) rather than once per cycle, deliberately: the
        counts have to include what this same cycle published a moment ago, or a
        single cycle could hand one user three cards against a cap of one.

        The third return value is the dedup verdict — the one rail the gate does not
        carry (see :func:`select_symbols`). ``None`` means "nothing in the way".

        ``pause`` composes the two rails M8.1 splits apart: the operator's
        system-wide ``/pause`` wins when active, because it is the wider statement;
        otherwise it is this user's own daily-loss pause.
        """
        config = effective_config(self._settings, stored)
        risk = config.risk
        now = self._clock.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        uid = user.telegram_user_id

        async with self._database.session() as session:
            fx = await self._repos.fx(session).get()
            system_pause = await self._repos.risk_state(session).load()
            signals = self._repos.signals(session)
            open_taken = await signals.open_taken(user_id=uid)
            open_symbols = await signals.open_symbols(user_id=uid)
            resolutions = await signals.resolutions_since(
                now - timedelta(hours=risk.signal_cooldown_hours), user_id=uid
            )
            today = await signals.published_since(day_start, user_id=uid)

        cooldowns = cooldown_until(resolutions, hours=risk.signal_cooldown_hours)
        account = account_state(user, config, fx.rate if fx is not None else Decimal("1"))
        plans = [TradePlan.model_validate(row.plan) for row in open_taken]
        portfolio = PortfolioState(
            open_risk_pct=open_risk_pct([plan.risk_per_trade_pct for plan in plans]),
            open_positions=len(plans),
            cooldown_until=cooldowns,
            pause=system_pause if system_pause.is_active(now) else user.pause,
            signals_today=today,
        )
        _, skipped = select_symbols(
            {symbol},
            open_symbols=open_symbols,
            cooldowns=cooldowns,
            published_today=today,
            max_per_day=risk.max_signals_per_day,
            now=now,
        )
        return account, portfolio, skipped.get(symbol)

    async def _history_block(self, symbol: str, *, owner_id: int | None) -> str:
        """specs/PROMPTS.md §3 — and from M7 both halves are real.

        Owner-scoped from M8.1 (owner ruling): one shared analysis produces one
        signal row per user, so counting all of them would multiply the sample size by
        the number of users and make the win rate a weighted average of everybody's
        execution. Without an owner row there is no calibration book, and both halves
        degrade to their "nothing measured yet" wording rather than to somebody else's
        numbers.
        """
        if owner_id is None:
            return build_history_block(symbol, [], [])
        async with self._database.session() as session:
            verdicts = await self._repos.reports(session).recent_for_symbol(
                symbol, limit=self._settings.config.llm.history_verdicts, owner_id=owner_id
            )
            stats = await setup_stats(session, now=self._clock.now(), owner_id=owner_id)
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
                skipped={s: skip.to_json() for s, skip in result.skipped.items()},
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
