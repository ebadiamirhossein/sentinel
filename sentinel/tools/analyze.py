"""M5 demo: the full LLM pipeline against live data.

    python -m sentinel.tools.analyze SOLUSDT             # deep analysis + gate
    python -m sentinel.tools.analyze --screen            # cheap triage over the watchlist
    python -m sentinel.tools.analyze --screen --analyze-candidates   # both tiers
    python -m sentinel.tools.analyze SOLUSDT --save      # + persist llm_calls / reports
    python -m sentinel.tools.analyze SOLUSDT --show-prompt   # print what the model receives

This is the only place in M5 that makes a live Anthropic call, and only when you
run it. It costs real money: one analyst call is roughly 20k input tokens
(three charts at ~2.1k visual tokens each, plus the snapshot) and the run prints
its own cost estimate so the number is never a mystery.

No trade execution, no exchange credentials, no order placement — the pipeline
ends at a printed plan for a human (PRD §3).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID, uuid4

from sentinel.analyst.history import build_history_block
from sentinel.analyst.models import AnalystReport, CandidateStatus
from sentinel.analyst.prompts.loader import load_prompt
from sentinel.analyst.providers.anthropic_fable import AnthropicFableAnalyst, order_charts
from sentinel.charts.models import ChartImage, ChartSpec
from sentinel.charts.renderer import render_album
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging, get_logger
from sentinel.core.wiring import assemble_with_features, snapshot_assembler
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import FxRate, MarketSnapshot
from sentinel.llm.client import AnthropicClient
from sentinel.llm.errors import AnalystUnavailable
from sentinel.llm.models import LLMCall
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import AccountState, GateDecision, MarketContext, PortfolioState
from sentinel.screener.models import ScreenerVerdict
from sentinel.screener.screener import Screener
from sentinel.storage.db import Database
from sentinel.storage.repositories import (
    AnalystReportRepository,
    FxRateRepository,
    LLMCallRepository,
    SnapshotRepository,
)
from sentinel.tools.size import render as render_decision

log = get_logger(__name__)


def chart_specs(settings: Settings, symbol: str) -> tuple[ChartSpec, ...]:
    charts = settings.config.charts
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


def render_report(report: AnalystReport) -> str:
    lines = [
        f"── {report.symbol} · {report.candidate_status.value} "
        f"[{report.setup_type.value} · {report.direction.value} · "
        f"{report.timeframe_label.value} · conf {report.confidence}] ──",
        f"prompt        {report.prompt_version} · model {report.model}",
        "",
        f"thesis        {report.thesis}",
        f"counter       {report.counter_thesis}",
    ]
    if report.entry_zone is not None:
        lines += [
            "",
            f"entry zone    {report.entry_zone.low} to {report.entry_zone.high}",
            f"stop          {report.stop}",
            "targets       " + ", ".join(str(target) for target in report.targets),
            f"invalidation  {report.invalidation_price} — {report.invalidation_text}",
        ]
    if report.evidence:
        lines += ["", "evidence"]
        lines += [f"  · {item.claim}  [{item.source_field}]" for item in report.evidence]
    if report.data_quality_note:
        lines += ["", f"data note     {report.data_quality_note}"]
    return "\n".join(lines) + "\n"


def cost_line(calls: list[LLMCall]) -> str:
    total = sum((call.cost_usd_estimate for call in calls), start=Decimal("0"))
    tokens_in = sum(call.usage.input_tokens for call in calls)
    tokens_out = sum(call.usage.output_tokens for call in calls)
    duration = sum(call.duration_ms for call in calls)
    return (
        f"llm           {len(calls)} call(s) · {tokens_in} in / {tokens_out} out · "
        f"~${total} · {duration}ms"
    )


async def analyze_symbol(
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    *,
    client: AnthropicClient,
    settings: Settings,
    database: Database | None,
    cycle_id: UUID,
    show_prompt: bool,
) -> tuple[AnalystReport | None, list[ChartImage], list[LLMCall]]:
    charts = list(render_album(snapshot, features, chart_specs(settings, snapshot.symbol)))

    verdicts = []
    if database is not None:
        async with database.session() as session:
            verdicts = await AnalystReportRepository(session).recent_for_symbol(
                snapshot.symbol, limit=settings.config.llm.history_verdicts
            )
    # setup_stats stays empty until M7's tracker measures outcomes; the block
    # says so in words rather than implying a result (specs/PROMPTS.md §3).
    history = build_history_block(snapshot.symbol, verdicts, [])

    analyst = AnthropicFableAnalyst(client, settings.config, cycle_id=cycle_id)

    if show_prompt:
        _print_prompt(analyst, snapshot, charts, history)

    try:
        report = await analyst.analyze(snapshot, charts, history)
    except AnalystUnavailable as exc:
        print(f"analyst unavailable for {snapshot.symbol}: {exc.reason} — {exc.detail}")
        return None, charts, analyst.calls
    return report, charts, analyst.calls


def _print_prompt(
    analyst: AnthropicFableAnalyst,
    snapshot: MarketSnapshot,
    charts: list[ChartImage],
    history: str,
) -> None:
    """Print exactly what the model will receive. Images as references, not base64.

    Prompt quality is what determines signal quality, so it has to be readable
    without a debugger or a database.
    """
    print("=" * 78)
    print("SYSTEM PROMPT")
    print("=" * 78)
    print(load_prompt(analyst.prompt_version))
    print()
    print("=" * 78)
    print("USER MESSAGE")
    print("=" * 78)
    for chart in order_charts(charts, snapshot.symbol):
        print(
            f"[IMAGE {chart.params.spec.timeframe}] sha256={chart.sha256[:16]} "
            f"{chart.params.spec.width_px}x{chart.params.spec.height_px} "
            f"candles={chart.params.candles_drawn}"
        )
    blocks = analyst.user_blocks(snapshot, order_charts(charts, snapshot.symbol), history)
    for block in blocks:
        if block["type"] == "text":
            print(block["text"])
            print()
    print("=" * 78)


def gate(report: AnalystReport, snapshot: MarketSnapshot, settings: Settings) -> GateDecision:
    features_model = None
    if snapshot.features:
        features_model = SymbolFeatures.model_validate(snapshot.features)
    return RiskEngine(settings.config).evaluate(
        report=report,
        market=MarketContext.from_snapshot(snapshot, features_model),
        account=AccountState(
            capital_eur=None if snapshot.fx is None else Decimal("10000"),
            eurusd_rate=None if snapshot.fx is None else snapshot.fx.rate,
        ),
        portfolio=PortfolioState(),
    )


async def run(
    symbols: list[str],
    *,
    settings: Settings,
    screen: bool,
    analyze_candidates: bool,
    save: bool,
    as_json: bool,
    show_prompt: bool,
) -> int:
    cycle_id = uuid4()
    database = Database(settings.secrets.database_url) if save else None
    key = settings.secrets.anthropic_api_key
    if key is None:
        print("ANTHROPIC_API_KEY is not set — nothing to call. See .env.example.")
        return 2

    client = AnthropicClient(settings.config.llm, api_key=key.get_secret_value())
    all_calls: list[LLMCall] = []
    reports: list[tuple[AnalystReport, MarketSnapshot]] = []

    async def last_known_good_fx() -> FxRate | None:
        if database is None:
            return None
        async with database.session() as session:
            return await FxRateRepository(session).get()

    try:
        async with snapshot_assembler(settings, last_known_good_fx=last_known_good_fx) as assembler:
            snapshots, feature_map = await assemble_with_features(
                assembler, symbols, settings.config.features, cycle_id=cycle_id
            )
            if not snapshots:
                print("no snapshots assembled — see the logs above")
                return 1

            targets = snapshots
            if screen:
                result = await Screener(client, settings.config, cycle_id=cycle_id).screen(
                    snapshots, feature_map
                )
                all_calls.extend(result.calls)
                print(_render_screener(result.verdicts))
                print(cost_line(list(result.calls)))
                print()
                if not analyze_candidates:
                    targets = []
                else:
                    interesting = {verdict.symbol for verdict in result.interesting}
                    targets = [s for s in snapshots if s.symbol in interesting]
                    if not targets:
                        print("no candidates this cycle — nothing to analyze deeply")

            for snapshot in targets:
                report, _charts, calls = await analyze_symbol(
                    snapshot,
                    feature_map[snapshot.symbol],
                    client=client,
                    settings=settings,
                    database=database,
                    cycle_id=cycle_id,
                    show_prompt=show_prompt,
                )
                all_calls.extend(calls)
                if report is None:
                    continue
                reports.append((report, snapshot))

                if as_json:
                    print(json.dumps(report.model_dump(mode="json"), indent=2))
                else:
                    print(render_report(report))
                print(cost_line(calls))
                print()

                if report.candidate_status is CandidateStatus.CANDIDATE:
                    print(render_decision(gate(report, snapshot, settings)))
                else:
                    print(
                        f"gate          skipped — analyst said "
                        f"{report.candidate_status.value}, not CANDIDATE\n"
                    )
    finally:
        await client.aclose()

    if save and database is not None:
        async with database.session() as session:
            for snapshot in snapshots:
                await SnapshotRepository(session).save(snapshot)
            await LLMCallRepository(session).record_many(all_calls)
            for report, snapshot in reports:
                await AnalystReportRepository(session).save(
                    report,
                    created_at=snapshot.captured_at,
                    provider=AnthropicFableAnalyst.name,
                    cycle_id=cycle_id,
                    snapshot_id=snapshot.snapshot_id,
                )
            await session.commit()
        print(f"saved {len(all_calls)} llm call(s), {len(reports)} report(s) · cycle_id={cycle_id}")

    if database is not None:
        await database.dispose()

    print(f"total         {cost_line(all_calls)}")
    return 0 if reports or screen else 1


def _render_screener(verdicts: Sequence[ScreenerVerdict]) -> str:
    lines = ["── screener ─────────────────────────────────────────────────"]
    for verdict in verdicts:
        mark = "★" if verdict.interesting else " "
        lines.append(
            f" {mark} {verdict.symbol:<10} {verdict.direction_hint.value:<8} {verdict.reason}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the M5 LLM pipeline against live data.")
    parser.add_argument("symbols", nargs="*", help="e.g. SOLUSDT; omit with --screen")
    parser.add_argument("--screen", action="store_true", help="run the cheap screener first")
    parser.add_argument(
        "--analyze-candidates",
        action="store_true",
        help="with --screen: deep-analyze whatever the screener marked interesting",
    )
    parser.add_argument("--save", action="store_true", help="persist LLM I/O and reports")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="print the assembled prompt before calling (images as references)",
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)

    symbols = [symbol.upper() for symbol in args.symbols]
    if not symbols:
        if not args.screen:
            parser.error("give at least one symbol, or use --screen")
        symbols = list(settings.config.watchlist)

    raise SystemExit(
        asyncio.run(
            run(
                symbols,
                settings=settings,
                screen=args.screen,
                analyze_candidates=args.analyze_candidates,
                save=args.save,
                as_json=args.json,
                show_prompt=args.show_prompt,
            )
        )
    )


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    main()
