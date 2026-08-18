"""Card rendering against specs/TELEGRAM_UX.md §1.

The plan under test is produced by the **real** risk engine (``tests/risk_double``)
rather than hand-assembled, so a renderer cannot pass by agreeing with a fixture
that never went through the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from sentinel.analyst.models import Direction
from sentinel.bot.cards import (
    positions_card,
    rejection_card,
    settings_card,
    signal_card,
    status_card,
    watchlist_card,
)
from sentinel.bot.models import SignalDecision, SignalRecord
from sentinel.bot.views import DataSourceView, SettingsView, StatusView
from sentinel.core.config import AppConfig
from sentinel.risk.models import GateStatus
from tests.risk_double import PLAN_NOW, account, analyst_report, approved_plan, decide

#: Telegram's hard limits — a card that exceeds either is not delivered at all.
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


def test_the_card_carries_every_section_the_spec_lists(record: SignalRecord, tz: ZoneInfo) -> None:
    card = signal_card(record, tz)
    for fragment in (
        "🟢 LONG — SOLUSDT",
        "trend_pullback · intraday · conf 78",
        "fable_v1 · Signal #1",
        "📊 <b>Thesis</b>",
        "⚠️ <b>Against:</b>",
        "Entry ladder (limit orders)",
        "% of risk",
        "Weighted entry",
        "🛑 <b>Stop:",
        "❌ Invalidation:",
        "🥅 TP1:",
        "💶 Notional",
        "Leverage 5x (isolated)",
        "🧾 Costs:",
        "Liq. buffer OK",
        "⚖️ Actual risk",
        "⏳ Expires if unfilled:",
    ):
        assert fragment in card, f"missing from the card: {fragment}"


def test_the_card_always_ends_with_the_disclaimer(record: SignalRecord, tz: ZoneInfo) -> None:
    """§6 — "Every card footer". The ILLUSTRATIVE example omitted it; the card must not."""
    card = signal_card(record, tz)
    assert card.rstrip().endswith(
        "<i>Research tool — not financial advice. Past stats ≠ future results.</i>"
    )


def test_rendering_is_deterministic(record: SignalRecord, tz: ZoneInfo) -> None:
    """No wall-clock in the rendering path — the same record renders identically."""
    assert signal_card(record, tz) == signal_card(record, tz)


def test_the_card_fits_a_telegram_message_but_not_a_caption(
    record: SignalRecord, tz: ZoneInfo
) -> None:
    """Why the album and the card are two messages, asserted rather than assumed.

    A media group cannot carry an inline keyboard *and* caps its caption at 1024
    characters. The card is comfortably over that and comfortably under the 4096
    message limit, so "charts as an album above the card" has to be two messages.
    """
    card = signal_card(record, tz)
    assert len(card) < TELEGRAM_TEXT_LIMIT
    assert len(card) > TELEGRAM_CAPTION_LIMIT


def test_timestamps_show_owner_local_and_utc(record: SignalRecord, tz: ZoneInfo) -> None:
    """specs/DATA_SOURCES.md §4 renders in owner time; the UTC figure keeps the
    card matchable to its audit row. Europe/Vilnius is UTC+3 in August, so a
    UTC-only regression cannot hide behind a zero offset."""
    card = signal_card(record, tz)
    assert "2026-08-18 15:00 EEST (12:00 UTC)" in card
    assert "2026-08-19 03:00 EEST (2026-08-19 00:00 UTC)" in card


def test_a_short_plan_renders_as_a_short(bot_config: AppConfig, tz: ZoneInfo) -> None:
    short = analyst_report(
        direction=Direction.SHORT,
        zone=("83.60", "84.60"),
        stop=("85.50"),
        targets=("81.60", "80.20", "78.00"),
    )
    plan = approved_plan(bot_config, report=short)
    card = signal_card(SignalRecord(plan=plan, number=2), tz)
    assert "🔴 SHORT — SOLUSDT" in card
    assert "🟢" not in card


def test_analyst_prose_is_escaped(record: SignalRecord, tz: ZoneInfo) -> None:
    """The counter-thesis contains "Fear & Greed"; HTML mode must not choke on it.

    M5 §4 established that news text is attacker-influenceable and sanitized before
    the model sees it. Escaping again on the way out means a thesis quoting a
    headline cannot break the markup even if that sanitizer is ever loosened.
    """
    card = signal_card(record, tz)
    assert "Fear &amp; Greed" in card
    assert "Fear & Greed" not in card


def test_a_decided_card_states_the_decision(record: SignalRecord, tz: ZoneInfo) -> None:
    decided = record.model_copy(update={"decision": SignalDecision.TAKEN})
    assert "<b>Your call: ✅ Taken</b>" in signal_card(decided, tz)


def test_a_plan_without_funding_says_so_rather_than_showing_zero(
    bot_config: AppConfig, tz: ZoneInfo
) -> None:
    """RISK_ENGINE §4.2 — "the card says funding n/a rather than a reassuring €0.00"."""
    from tests.risk_double import market_context

    plan = approved_plan(bot_config, market=market_context(funding_rate=None))
    card = signal_card(SignalRecord(plan=plan, number=3), tz)
    assert "funding n/a — no funding rate in the snapshot" in card
    assert "funding ~€0.00" not in card


def test_a_rejection_names_a_code_and_prose(bot_config: AppConfig, tz: ZoneInfo) -> None:
    decision = decide(bot_config, account=account(capital_eur=None))
    assert decision.status is GateStatus.REJECTED
    card = rejection_card(decision, tz, PLAN_NOW)
    assert "NO_CAPITAL" in card
    assert "capital_eur is not set" in card


# --------------------------------------------------------------------------- #
# Command output
# --------------------------------------------------------------------------- #


def _status(**overrides: object) -> StatusView:
    base: dict[str, object] = {
        "paused": False,
        "pause_reason": None,
        "paused_until": None,
        "capital_eur": Decimal("10000"),
        "risk_per_trade_pct": Decimal("0.75"),
        "watchlist_size": 10,
        "signals_total": 3,
        "signals_undecided": 1,
        "signals_taken": 2,
        "stuck_messages": 0,
    }
    base.update(overrides)
    return StatusView(**base)  # type: ignore[arg-type]


def test_status_names_what_it_cannot_measure_yet(tz: ZoneInfo) -> None:
    """Ruling 1: degrade explicitly. No fabricated cycle time, no zeroed uPnL."""
    card = status_card(_status(), tz)
    assert "▶️ Active — not paused." in card
    assert "capital: €10000" in card
    assert "no snapshot stored yet" in card
    assert "arrive with the orchestrator and tracker at M7" in card


def test_status_shouts_when_capital_is_unset(tz: ZoneInfo) -> None:
    """An unset capital rejects every signal, so /status must not whisper it."""
    card = status_card(_status(capital_eur=None), tz)
    assert "not set" in card
    assert "NO_CAPITAL" in card


def test_status_reports_a_pause_and_degraded_sources(tz: ZoneInfo) -> None:
    card = status_card(
        _status(
            paused=True,
            pause_reason="DAILY_LOSS_LIMIT",
            paused_until=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
            stuck_messages=1,
            data_sources=(
                DataSourceView("BTCUSDT", "OK", PLAN_NOW),
                DataSourceView("ETHUSDT", "DEGRADED", PLAN_NOW, ("fear_greed",)),
            ),
        ),
        tz,
    )
    assert "⏸️ <b>PAUSED</b> — DAILY_LOSS_LIMIT" in card
    assert "✅ BTCUSDT OK" in card
    assert "⚠️ ETHUSDT DEGRADED (fear_greed)" in card
    assert "claimed but never confirmed" in card


def test_positions_is_empty_rather_than_invented(tz: ZoneInfo) -> None:
    assert "Nothing marked ✅ Taken yet." in positions_card([], tz)


def test_settings_tags_every_value_with_its_source() -> None:
    view = SettingsView(
        groups=(("Sizing", (("capital_eur", "€10000", "db"), ("max_leverage", "10x", "yaml"))),)
    )
    card = settings_card(view)
    assert "capital_eur: €10000 <i>[db]</i>" in card
    assert "max_leverage: 10x <i>[yaml]</i>" in card


def test_watchlist_lists_symbols_and_its_source() -> None:
    card = watchlist_card(("BTCUSDT", "SOLUSDT"), "db")
    assert "(2 symbols) <i>[db]</i>" in card
    assert "· SOLUSDT" in card


@pytest.mark.parametrize("count", [0, 1, 3])
def test_the_card_handles_any_legal_number_of_targets(
    bot_config: AppConfig, tz: ZoneInfo, count: int
) -> None:
    """1-3 targets per PROMPTS §2; the four target tuples must stay parallel."""
    if count == 0:
        pytest.skip("the gate rejects a plan with no targets — covered in tests/risk")
    targets = ("85.20", "86.60", "88.90")[:count]
    plan = approved_plan(bot_config, report=analyst_report(targets=targets))
    card = signal_card(SignalRecord(plan=plan, number=4), tz)
    assert f"🥅 TP{count}:" in card
    assert f"🥅 TP{count + 1}:" not in card


# --------------------------------------------------------------------------- #
# The spec's own example
# --------------------------------------------------------------------------- #


def test_the_spec_example_card_matches_what_the_tool_renders() -> None:
    """specs/TELEGRAM_UX.md §1 is generated, so it must not drift from the code.

    The §1 block was hand-written until M6 and had stopped reconciling with itself
    (M4_REPORT §4.3) — the failure mode a spec example has when nothing regenerates
    it. This test is what stops that happening again: change the renderer without
    re-running ``--doc`` and the suite says so.
    """
    from pathlib import Path

    from sentinel.core.config import load_settings
    from sentinel.tools.signal import DOC_NOW, doc_block, evaluate

    root = Path(__file__).resolve().parents[2]
    settings = load_settings(root / "config.yaml")
    decision = evaluate(root / "examples" / "sol_long.json", settings, DOC_NOW)
    rendered = doc_block(decision, settings)

    spec = (root / "docs" / "specs" / "TELEGRAM_UX.md").read_text(encoding="utf-8")
    assert rendered in spec, (
        "specs/TELEGRAM_UX.md §1 is out of date — regenerate it with:\n"
        "    python -m sentinel.tools.signal --fixture examples/sol_long.json --doc"
    )
