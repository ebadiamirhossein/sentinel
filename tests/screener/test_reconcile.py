"""Reconciliation: the deterministic step the model gets no say in.

Pure -- no client, no cassette. This is the function that stops a hallucinated
symbol reaching the deep analyst, so it is worth testing on its own rather than
only through a batch call.
"""

from __future__ import annotations

import structlog

from sentinel.screener.models import DirectionHint, ScreenerVerdict
from sentinel.screener.screener import reconcile

# ── reconciliation is pure and testable on its own ─────────────────────────


def test_missing_symbol_defaults_to_not_interesting() -> None:
    """An unanswered symbol is not an interesting one."""
    returned = (
        ScreenerVerdict(
            symbol="BTCUSDT", interesting=True, direction_hint=DirectionHint.LONG, reason="ok"
        ),
    )
    result = reconcile(["BTCUSDT", "ETHUSDT"], returned)

    assert [v.symbol for v in result] == ["BTCUSDT", "ETHUSDT"]
    assert result[1].interesting is False
    assert "no verdict returned" in result[1].reason


def test_hallucinated_symbol_is_dropped() -> None:
    """A symbol with no snapshot cannot be analyzed even if it were interesting."""
    returned = (
        ScreenerVerdict(
            symbol="DOGEUSDT", interesting=True, direction_hint=DirectionHint.LONG, reason="moon"
        ),
    )
    result = reconcile(["BTCUSDT"], returned)

    assert [v.symbol for v in result] == ["BTCUSDT"]
    assert result[0].interesting is False


def test_output_order_follows_submission_order() -> None:
    returned = (
        ScreenerVerdict(
            symbol="ETHUSDT", interesting=False, direction_hint=DirectionHint.UNCLEAR, reason="-"
        ),
        ScreenerVerdict(
            symbol="BTCUSDT", interesting=True, direction_hint=DirectionHint.SHORT, reason="-"
        ),
    )
    assert [v.symbol for v in reconcile(["BTCUSDT", "ETHUSDT"], returned)] == [
        "BTCUSDT",
        "ETHUSDT",
    ]


def test_case_mismatch_is_matched_but_our_spelling_wins() -> None:
    returned = (
        ScreenerVerdict(
            symbol="btcusdt", interesting=True, direction_hint=DirectionHint.LONG, reason="ok"
        ),
    )
    result = reconcile(["BTCUSDT"], returned)
    assert result[0].symbol == "BTCUSDT"
    assert result[0].interesting is True


def test_reconciliation_logs_both_kinds_of_mismatch() -> None:
    returned = (
        ScreenerVerdict(
            symbol="DOGEUSDT", interesting=True, direction_hint=DirectionHint.LONG, reason="-"
        ),
    )
    with structlog.testing.capture_logs() as logs:
        reconcile(["BTCUSDT"], returned)

    events = {line["event"] for line in logs}
    assert {"screener.unknown_symbols", "screener.missing_symbols"} <= events
