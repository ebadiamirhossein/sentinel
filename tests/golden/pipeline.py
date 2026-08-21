"""The deterministic pipeline, driven end to end from recorded inputs.

M10a's control. Ingestion → features → charts → prompt assembly → risk gate →
signal card, with **no network, no clock and no database**: the OHLCV comes from
``tests/cassettes``, the analyst's answer comes from a stored payload rather than
from a model, and every timestamp is frozen. Everything that remains is a pure
function of committed bytes, which is exactly what makes byte-identity a fair
assertion rather than a flaky one.

The analyst call itself is *not* made — an LLM is not deterministic and never will
be. What is captured instead is the thing M10a could plausibly break: the **prompt
assembled for it**, which is a pure function of the snapshot, the charts and the
history block.

Why cassettes rather than an export from the live database (owner ruling,
2026-08-20): the cassettes are real recorded Binance responses, they drive the
identical code path, and they need no production access for the test to run in CI
from the first commit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sentinel.analyst.history import build_history_block
from sentinel.analyst.models import AnalystReport, AnalystReportPayload
from sentinel.analyst.prompts.loader import load_prompt
from sentinel.analyst.providers.anthropic_fable import AnthropicFableAnalyst, order_charts
from sentinel.bot.cards import signal_card
from sentinel.bot.models import SignalRecord
from sentinel.charts.models import ChartImage, ChartSpec
from sentinel.charts.renderer import render_album
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.core.markets import Market
from sentinel.features import compute as compute_features
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import InstrumentMeta, MarketSnapshot
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import (
    AccountState,
    GateDecision,
    MarketContext,
    PortfolioState,
)
from tests.anthropic_double import Recorder, make_client, scripted_transport
from tests.market_double import snapshot_from_cassettes

#: Frozen "now" for the whole golden run. Every expiry, watermark and card
#: timestamp derives from it, so nothing in a golden depends on the wall clock.
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: The owner's display timezone, as ``config.yaml`` sets it. Hardcoded rather than
#: read from config so a config edit cannot silently rewrite a golden.
TZ = ZoneInfo("Europe/Vilnius")

SYMBOL = "BTCUSDT"

#: The golden symbols beyond :data:`SYMBOL` (M10b-2, owner requirement H1). Both are
#: on the live watchlist, and each sits in a **different** branch of
#: ``charts/renderer._format_price`` — SOLUSDT between 1 and 1000, DOGEUSDT below 1,
#: where BTCUSDT's price is above 1000. See :func:`golden_symbol_charts`.
#:
#: One symbol per branch, and a test asserts that is still true, so the set cannot
#: quietly collapse into covering one branch three times.
EXTRA_SYMBOLS = ("SOLUSDT", "DOGEUSDT")

#: Recorded live in M1 — see ``tests/cassettes/binance_market_BTCUSDT.json``.
#: BTCUSDT's exchange minimum is 50 USDT, above the engine's 20 USDT floor, which
#: is precisely why this symbol is the one worth pinning.
INSTRUMENT = InstrumentMeta(
    source="binance_usdm",
    fetched_at=NOW,
    symbol=SYMBOL,
    tick_size=Decimal("0.1"),
    qty_step=Decimal("0.001"),
    min_notional=Decimal("50"),
)

#: The account the plan is sized against. Not the owner's real capital (€200): a
#: golden that moved when the owner typed /capital would be a golden of the wrong
#: thing. €10,000 is the figure every other suite in this repo sizes against.
CAPITAL_EUR = Decimal("10000")
EURUSD = Decimal("1.1593")

#: An approved setup, anchored on the cassette's real last price of 64100.0 so the
#: entry-distance, ATR-bounds, margin-budget and net-RR rules are all exercised on
#: live-shaped numbers rather than on a toy.
#:
#: The stop is deliberately ~2.6x ATR(14,1h) (209.27) rather than something tighter.
#: §4's ``margin_budget_pct`` is a **hard** cap from M8.2, and it binds wherever
#: ``stop_fraction < risk_per_trade_pct``: at 0.75% risk, 10x leverage and a 10%
#: budget, any stop closer than 0.75% of entry is rejected with
#: MARGIN_BUDGET_EXCEEDED before RR is ever considered. This one sits inside both
#: the ATR band and the margin budget, and away from either edge, so an unrelated
#: change fails the *feature* golden rather than silently flipping the gate.
APPROVED_REPORT: dict[str, Any] = {
    "symbol": SYMBOL,
    "entry_zone": {"low": 63800.0, "high": 64150.0},
    "stop": 63450.0,
    "targets": [65100.0, 65900.0, 67000.0],
    "invalidation_price": 63450.0,
    "invalidation_text": "1h close below 63450 breaks the demand shelf.",
    "confidence": 74,
}

#: The rejection path. Identical to the approval except TP1, moved close enough
#: that gross RR is 1.63 and **net** RR is 1.39 — over §2 rule 5's 1.5 threshold on
#: the flattering reading and under it once §4.2's fees and funding are charged.
#:
#: NET_RR_TOO_LOW deliberately, out of the ~24 codes available: it is the rejection
#: the 2026-08-18 net-RR correction created, it is the one that tightened the live
#: gate materially, and reaching it means the whole cost engine ran. A geometry
#: rejection would have been cheaper to write and would have proved far less.
REJECTED_REPORT: dict[str, Any] = {
    **APPROVED_REPORT,
    "targets": [64900.0, 65900.0, 67000.0],
}


def golden_snapshot(symbol: str = SYMBOL) -> MarketSnapshot:
    """The recorded cycle's input, with the exchange rules the gate needs.

    ``snapshot_from_cassettes`` carries OHLCV and nothing else; the gate rejects
    with ``INSTRUMENT_META_MISSING`` without trading rules, so they are attached
    from the recorded market cassette rather than invented.

    The instrument rules are BTCUSDT's and are only used by the gate, which the
    second symbol does not run — see :func:`golden_symbol_charts`.
    """
    snapshot = snapshot_from_cassettes(symbol)
    if symbol != SYMBOL:
        return snapshot
    return snapshot.model_copy(update={"instrument": INSTRUMENT})


def golden_features(snapshot: MarketSnapshot, config: AppConfig) -> SymbolFeatures:
    return compute_features(snapshot, config.features)


def chart_specs(config: AppConfig, symbol: str = SYMBOL) -> tuple[ChartSpec, ...]:
    """The same specs the orchestrator builds — see ``_chart_specs`` there."""
    charts = config.charts
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


def golden_charts(
    snapshot: MarketSnapshot, features: SymbolFeatures, config: AppConfig
) -> tuple[ChartImage, ...]:
    """The **real** renderer, not ``tests/market_double.fake_chart``.

    A stand-in would make the chart assertions vacuous, and the chart parameters
    are one of the four things this milestone is forbidden to change.
    """
    return render_album(snapshot, features, chart_specs(config, snapshot.symbol))


def golden_report(payload: dict[str, Any], config: AppConfig) -> AnalystReport:
    """A stored analyst answer, validated through the real schema on the way in."""
    from tests.market_double import valid_report_json

    validated = AnalystReportPayload.model_validate_json(valid_report_json(**payload))
    return validated.to_report(prompt_version="fable_v1", model=config.llm.analyst_model)


def golden_prompt(
    snapshot: MarketSnapshot, charts: tuple[ChartImage, ...], config: AppConfig
) -> str:
    """Exactly what the analyst would be sent, as text.

    The client is real but its transport is never reached: ``user_blocks`` makes no
    request. Building one anyway keeps this on the production code path instead of
    a hand-rolled copy of it that could drift.
    """
    client = make_client(scripted_transport([], Recorder()), config)
    analyst = AnthropicFableAnalyst(client, config)
    ordered = order_charts(list(charts), snapshot.symbol)
    history = build_history_block(snapshot.symbol, [], [])
    blocks = analyst.user_blocks(snapshot, ordered, history)

    lines = [f"SYSTEM\n{load_prompt(analyst.prompt_version)}", ""]
    for block in blocks:
        if block["type"] == "text":
            lines.append(block["text"])
        else:
            # Images are pinned by digest in charts.json; repeating ~1.6MB of
            # base64 here would make the prompt golden unreadable and would
            # duplicate an assertion that already exists.
            lines.append("[image block]")
        lines.append("")
    return "\n".join(lines)


def golden_gate(
    report: AnalystReport,
    snapshot: MarketSnapshot,
    features: SymbolFeatures,
    config: AppConfig,
) -> GateDecision:
    return RiskEngine(config, clock=FrozenClock(NOW)).evaluate(
        report=report,
        market=MarketContext.from_snapshot(snapshot, features),
        account=AccountState(
            capital_eur=CAPITAL_EUR,
            risk_per_trade_pct=config.risk.risk_per_trade_pct,
            eurusd_rate=EURUSD,
        ),
        portfolio=PortfolioState(),
    )


def golden_card(decision: GateDecision, *, show_market: bool = False) -> str:
    """The card as the owner reads it. ``number`` is fixed — Postgres assigns it.

    ``show_market`` defaults to **off**, which is the form the shipped config renders
    and the form ``card.txt`` pins. The tagged form is pinned separately, in
    ``card_multi.txt``, so switch-on day is a config flip rather than a regeneration
    (FOREX.md defect #23).
    """
    assert decision.plan is not None
    record = SignalRecord(plan=decision.plan, user_id=7222549221, number=42)
    return signal_card(record, TZ, show_market=show_market)


def multi_market(config: AppConfig) -> AppConfig:
    """The shipped config with forex switched on — **in memory only**.

    The same helper ``tests/bot/test_multi_market.py`` has had since M10a, here so the
    multi-market goldens are produced from the config that will actually exist on
    switch-on day rather than from a config invented for the fixture.
    """
    forex = config.market(Market.FOREX).model_copy(update={"enabled": True})
    return config.model_copy(update={"markets": {**config.markets, Market.FOREX: forex}})


@dataclass(frozen=True)
class GoldenCycle:
    """One run's captured artefacts, in the order the pipeline produces them."""

    features: dict[str, Any]
    charts: dict[str, Any]
    prompt: str
    gate_approved: dict[str, Any]
    gate_rejected: dict[str, Any]
    card: str


def gate_json(decision: GateDecision) -> dict[str, Any]:
    """A gate verdict as comparable JSON.

    ``plan_id`` is dropped: it is a ``uuid4`` default, it appears on no surface a
    human ever sees, and leaving it in would make the golden fail every run for a
    reason that means nothing.
    """
    dumped: dict[str, Any] = json.loads(json.dumps(decision.model_dump(mode="json")))
    plan = dumped.get("plan")
    if isinstance(plan, dict):
        plan.pop("plan_id", None)
    return dumped


def run_golden_cycle(config: AppConfig) -> GoldenCycle:
    """Ingest → features → charts → prompt → gate → card, from committed bytes."""
    snapshot = golden_snapshot()
    features = golden_features(snapshot, config)
    charts = golden_charts(snapshot, features, config)
    approved = golden_gate(golden_report(APPROVED_REPORT, config), snapshot, features, config)
    rejected = golden_gate(golden_report(REJECTED_REPORT, config), snapshot, features, config)

    return GoldenCycle(
        features=json.loads(json.dumps(features.model_dump(mode="json"))),
        charts={
            chart.params.spec.timeframe: {
                "sha256": chart.sha256,
                "params": chart.params.to_json_dict(),
            }
            for chart in charts
        },
        prompt=golden_prompt(snapshot, charts, config),
        gate_approved=gate_json(approved),
        gate_rejected=gate_json(rejected),
        card=golden_card(approved),
    )


def golden_symbol_charts(config: AppConfig, symbol: str) -> dict[str, Any]:
    """Features and chart digests for one symbol, without the gate or the card.

    **Why more than one symbol exists (owner requirement H1, 2026-08-21).**
    ``charts.json`` pinned BTCUSDT and nothing else, and BTCUSDT trades above 1000.
    ``charts/renderer._format_price`` — which labels every S/R line — has three
    branches, at 1000 and at 1, so one symbol pinned **one** of three. A change to
    either other branch would have moved the stored bytes of live watchlist symbols
    — LINK, AVAX and LTC between 1 and 1000; XRP, DOGE and ADA below 1 — and this
    suite would have stayed green. M10b-2 found that hole by needing the formatter
    to behave differently for forex.

    Deliberately not the gate or the card. ``bot/cards.py`` interpolates prices the
    risk engine has already quantized, so nothing there is magnitude-sensitive, and a
    second hand-tuned analyst report would add a fragile golden without covering the
    hole this exists to close.
    """
    snapshot = golden_snapshot(symbol)
    features = golden_features(snapshot, config)
    charts = golden_charts(snapshot, features, config)
    return {
        "features": json.loads(json.dumps(features.model_dump(mode="json"))),
        "charts": {
            chart.params.spec.timeframe: {
                "sha256": chart.sha256,
                "params": chart.params.to_json_dict(),
            }
            for chart in charts
        },
    }


__all__ = [
    "APPROVED_REPORT",
    "CAPITAL_EUR",
    "EURUSD",
    "EXTRA_SYMBOLS",
    "INSTRUMENT",
    "NOW",
    "REJECTED_REPORT",
    "SYMBOL",
    "TZ",
    "GoldenCycle",
    "golden_symbol_charts",
    "run_golden_cycle",
]
