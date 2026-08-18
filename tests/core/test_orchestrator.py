"""The cycle's guards, its bookkeeping, and what dry run actually costs.

The stages themselves are tested where they live (M1's ingestion, M3's charts,
M5's screener and analyst, M4's gate). What is new here is the *decisions the
orchestrator makes about them*: which candidates are worth an analyst call, when
the expensive tier is held back, and what happens to an approved plan when the
system is only rehearsing.
"""

from __future__ import annotations

from datetime import timedelta

from sentinel.core.orchestrator import select_symbols

from .conftest import NOW


def selected(
    *symbols: str,
    open_symbols: set[str] | None = None,
    cooldowns: dict[str, object] | None = None,
    published_today: int = 0,
    max_per_day: int = 5,
) -> tuple[set[str], dict[str, str]]:
    return select_symbols(
        set(symbols),
        open_symbols=open_symbols or set(),
        cooldowns=cooldowns or {},  # type: ignore[arg-type]
        published_today=published_today,
        max_per_day=max_per_day,
        now=NOW,
    )


# --------------------------------------------------------------------------- #
# Dedup — PRD F11's "max 1 active signal per symbol"
# --------------------------------------------------------------------------- #


def test_a_symbol_with_a_live_signal_is_not_analysed_again() -> None:
    """PRD §3: "One trade plan at a time per symbol". Analysing it again would
    cost ~$0.32 to produce a plan the gate rejects anyway."""
    allowed, skipped = selected("SOLUSDT", "ETHUSDT", open_symbols={"SOLUSDT"})
    assert allowed == {"ETHUSDT"}
    assert "already open" in skipped["SOLUSDT"]


def test_the_reason_is_recorded_and_not_only_the_exclusion() -> None:
    """A cycle that analysed nothing must be explicable from its own row. "Zero
    candidates" and "three candidates, all on cooldown" are very different days."""
    _, skipped = selected("SOLUSDT", open_symbols={"SOLUSDT"})
    assert skipped["SOLUSDT"], "a dropped symbol must say why"


# --------------------------------------------------------------------------- #
# Cooldown — ARCHITECTURE §3 step 6
# --------------------------------------------------------------------------- #


def test_a_symbol_on_cooldown_is_held_back() -> None:
    allowed, skipped = selected("SOLUSDT", cooldowns={"SOLUSDT": NOW + timedelta(hours=2)})
    assert allowed == set()
    assert "on cooldown until" in skipped["SOLUSDT"]


def test_an_expired_cooldown_lets_the_symbol_through() -> None:
    allowed, _ = selected("SOLUSDT", cooldowns={"SOLUSDT": NOW - timedelta(minutes=1)})
    assert allowed == {"SOLUSDT"}


# --------------------------------------------------------------------------- #
# The daily cap — specs/TELEGRAM_UX.md §6
# --------------------------------------------------------------------------- #


def test_the_daily_cap_counts_what_this_cycle_would_add() -> None:
    """Four already published and three candidates: only one may go through.

    Counting only what is *already* stored would let a single cycle publish three
    more and blow the cap by two, which is exactly the spam §6 forbids.
    """
    allowed, skipped = selected("SOLUSDT", "ETHUSDT", "BTCUSDT", published_today=4, max_per_day=5)
    assert len(allowed) == 1
    assert len(skipped) == 2
    assert all("daily signal cap" in reason for reason in skipped.values())


def test_a_full_day_analyses_nothing() -> None:
    allowed, skipped = selected("SOLUSDT", published_today=5, max_per_day=5)
    assert allowed == set()
    assert "daily signal cap reached (5)" in skipped["SOLUSDT"]


def test_a_quiet_day_lets_everything_through() -> None:
    allowed, skipped = selected("SOLUSDT", "ETHUSDT")
    assert allowed == {"SOLUSDT", "ETHUSDT"}
    assert skipped == {}


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #


def test_the_most_specific_reason_wins() -> None:
    """A symbol that is both open and capped reads better as "already open" — the
    cap is a property of the day, the open signal is a property of the symbol."""
    _, skipped = selected("SOLUSDT", open_symbols={"SOLUSDT"}, published_today=9, max_per_day=5)
    assert "already open" in skipped["SOLUSDT"]


def test_selection_is_deterministic_when_the_cap_bites() -> None:
    """Candidates are considered in sorted order, so which one survives a
    part-full day does not depend on set iteration order."""
    first, _ = selected("SOLUSDT", "BTCUSDT", "ETHUSDT", published_today=4)
    second, _ = selected("ETHUSDT", "BTCUSDT", "SOLUSDT", published_today=4)
    assert first == second == {"BTCUSDT"}
