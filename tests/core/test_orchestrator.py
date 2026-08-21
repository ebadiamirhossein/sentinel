"""The cycle's guards, its bookkeeping, and what dry run actually costs.

The stages themselves are tested where they live (M1's ingestion, M3's charts,
M5's screener and analyst, M4's gate). What is new here is the *decisions the
orchestrator makes about them*: which candidates are worth an analyst call, when
the expensive tier is held back, and what happens to an approved plan when the
system is only rehearsing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from sentinel.core import orchestrator as orchestrator_module
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Settings
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.core.orchestrator import (
    CycleOrchestrator,
    CycleRepositories,
    CycleResult,
    SkipReason,
    select_symbols,
)
from sentinel.llm.models import LLMCall, LLMCallKind, LLMCallStatus
from sentinel.llm.spend import SpendState, SpendTotals

from .conftest import NOW, CycleDatabase, CycleStore, CycleUsers


def selected(
    *symbols: str,
    open_symbols: set[str] | None = None,
    cooldowns: dict[str, object] | None = None,
    published_today: int = 0,
    max_per_day: int = 5,
    recently_analysed: dict[str, Any] | None = None,
) -> tuple[set[str], dict[str, Any]]:
    return select_symbols(
        set(symbols),
        open_symbols=open_symbols or set(),
        cooldowns=cooldowns or {},  # type: ignore[arg-type]
        published_today=published_today,
        max_per_day=max_per_day,
        now=NOW,
        recently_analysed=recently_analysed,
    )


# --------------------------------------------------------------------------- #
# The re-analysis cooldown — M8.2
# --------------------------------------------------------------------------- #


def test_a_symbol_analysed_recently_and_not_a_candidate_is_held_back() -> None:
    """The M8.2 rail. LINKUSDT was escalated in 8 of 8 cycles on 2026-08-19 and came
    back WATCHLIST every time, at ~$0.28 a look, on a 1h setup timeframe whose candle
    had not closed between any two of them."""
    allowed, skipped = selected(
        "LINKUSDT", "ETHUSDT", recently_analysed={"LINKUSDT": NOW + timedelta(minutes=45)}
    )
    assert allowed == {"ETHUSDT"}
    assert skipped["LINKUSDT"].reason is SkipReason.RECENTLY_ANALYSED


def test_the_re_analysis_cooldown_lapses() -> None:
    """It is a cooldown, not a ban — the symbol comes back when the candle closes."""
    allowed, _ = selected("LINKUSDT", recently_analysed={"LINKUSDT": NOW - timedelta(minutes=1)})
    assert allowed == {"LINKUSDT"}


def test_a_suppressed_symbol_does_not_consume_a_daily_cap_slot() -> None:
    """Ordering, asserted rather than assumed.

    A symbol held back as already-analysed was never going to become a signal, so
    charging it against ``max_signals_per_day`` would let the cheapest guard in the
    system spend the day's budget on symbols nobody was ever offered.
    """
    allowed, skipped = selected(
        "AAAUSDT",
        "BBBUSDT",
        recently_analysed={"AAAUSDT": NOW + timedelta(minutes=45)},
        published_today=4,
        max_per_day=5,
    )
    assert allowed == {"BBBUSDT"}
    assert skipped["AAAUSDT"].reason is SkipReason.RECENTLY_ANALYSED


def test_an_open_signal_outranks_the_re_analysis_cooldown() -> None:
    """Both are true; the more specific finding is the one M9 should count."""
    _, skipped = selected(
        "SOLUSDT",
        open_symbols={"SOLUSDT"},
        recently_analysed={"SOLUSDT": NOW + timedelta(minutes=45)},
    )
    assert skipped["SOLUSDT"].reason is SkipReason.OPEN_SIGNAL


# --------------------------------------------------------------------------- #
# Dedup — PRD F11's "max 1 active signal per symbol"
# --------------------------------------------------------------------------- #


def test_a_symbol_with_a_live_signal_is_not_analysed_again() -> None:
    """PRD §3: "One trade plan at a time per symbol". Analysing it again would
    cost ~$0.32 to produce a plan the gate rejects anyway."""
    allowed, skipped = selected("SOLUSDT", "ETHUSDT", open_symbols={"SOLUSDT"})
    assert allowed == {"ETHUSDT"}
    assert skipped["SOLUSDT"].reason is SkipReason.OPEN_SIGNAL
    assert "already open" in skipped["SOLUSDT"].detail


def test_the_reason_is_recorded_and_not_only_the_exclusion() -> None:
    """A cycle that analysed nothing must be explicable from its own row. "Zero
    candidates" and "three candidates, all on cooldown" are very different days."""
    _, skipped = selected("SOLUSDT", open_symbols={"SOLUSDT"})
    assert skipped["SOLUSDT"].detail, "a dropped symbol must say why"


# --------------------------------------------------------------------------- #
# Cooldown — ARCHITECTURE §3 step 6
# --------------------------------------------------------------------------- #


def test_a_symbol_on_cooldown_is_held_back() -> None:
    allowed, skipped = selected("SOLUSDT", cooldowns={"SOLUSDT": NOW + timedelta(hours=2)})
    assert allowed == set()
    assert skipped["SOLUSDT"].reason is SkipReason.COOLDOWN
    assert "on cooldown until" in skipped["SOLUSDT"].detail


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
    assert all(skip.reason is SkipReason.DAILY_CAP for skip in skipped.values())


def test_a_full_day_analyses_nothing() -> None:
    allowed, skipped = selected("SOLUSDT", published_today=5, max_per_day=5)
    assert allowed == set()
    assert skipped["SOLUSDT"].reason is SkipReason.DAILY_CAP
    assert "daily signal cap reached (5)" in skipped["SOLUSDT"].detail


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
    assert skipped["SOLUSDT"].reason is SkipReason.OPEN_SIGNAL
    assert "already open" in skipped["SOLUSDT"].detail


def test_selection_is_deterministic_when_the_cap_bites() -> None:
    """Candidates are considered in sorted order, so which one survives a
    part-full day does not depend on set iteration order."""
    first, _ = selected("SOLUSDT", "BTCUSDT", "ETHUSDT", published_today=4)
    second, _ = selected("ETHUSDT", "BTCUSDT", "SOLUSDT", published_today=4)
    assert first == second == {"BTCUSDT"}


# --------------------------------------------------------------------------- #
# The spend guard's two readings (M8)
#
# The guard itself shipped in M7 and is tested in tests/llm/test_spend.py. What
# is new is *when* the cycle reads it: once before it spends anything, once after
# its calls are committed. The pair is what lets the admin alert fire on the
# crossing rather than needing an "already warned today" row somewhere — see
# sentinel/bot/alerts.py and tests/bot/test_admin_alerts.py.
# --------------------------------------------------------------------------- #


class _Verdicts:
    """What ``Screener.screen`` returns, reduced to the two attributes used."""

    def __init__(self, calls: list[LLMCall]) -> None:
        self.calls = calls
        self.interesting: list[object] = []


class _Screener:
    """A screener that finds nothing interesting and bills for looking."""

    def __init__(self, client: object, config: object, *, cycle_id: object) -> None:
        self._call = LLMCall(
            kind=LLMCallKind.SCREENER,
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_version="screener_v1",
            status=LLMCallStatus.OK,
            cost_usd_estimate=Decimal("4.00"),
            started_at=NOW,
        )

    async def screen(self, snapshots: object, features: object) -> _Verdicts:
        return _Verdicts([self._call])


class _Signals:
    """The three reads the pre-analyst guards make — grouped by user from M8.1."""

    def __init__(self, session: object) -> None: ...

    async def open_symbols_by_user(self) -> dict[int, set[str]]:
        return {}

    async def resolutions_by_user_since(
        self, since: datetime
    ) -> dict[int, list[tuple[str, datetime]]]:
        return {}

    async def published_by_user_since(self, since: datetime) -> dict[int, int]:
        return {}


class _LLMCalls:
    """Spend that actually accumulates, so "after" means after.

    A fake returning a constant would pass whether the second reading happened
    before or after the commit — which is the only thing this is testing.
    """

    def __init__(self, session: Any, *, market: Market = LEGACY_MARKET) -> None:
        self._store: CycleStore = session.store

    async def record_many(self, calls: Sequence[LLMCall]) -> int:
        self._store.llm_calls.extend(calls)
        self._store.spend_day += sum((call.cost_usd_estimate for call in calls), Decimal(0))
        return len(calls)

    async def spend_totals(self, **_: object) -> SpendTotals:
        return SpendTotals(day_usd=self._store.spend_day, month_usd=self._store.spend_day)

    async def spend_totals_across_markets(self, **_: object) -> SpendTotals:
        """One market in this fake, so the deployment total is the market total.

        Which is exactly the production situation with forex disabled, and the
        reason the two-tier guard returns what the single-tier one used to.
        """
        return SpendTotals(day_usd=self._store.spend_day, month_usd=self._store.spend_day)

    async def day_spend_by_market(self, **_: object) -> dict[Market, Decimal]:
        """M10b-2's reserved-floor input. One market in this fake, so the breakdown is
        the same figure once more — and crypto's floor never reserves against crypto."""
        return {Market.CRYPTO: self._store.spend_day}


async def _spend_states(store: CycleStore, settings: Settings) -> CycleResult:
    result = CycleResult(cycle_id=uuid4())
    orchestrator = CycleOrchestrator(
        settings,
        CycleDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
        repositories=CycleRepositories(
            signals=_Signals,  # type: ignore[arg-type]
            llm_calls=_LLMCalls,  # type: ignore[arg-type]
            users=CycleUsers,  # type: ignore[arg-type]
        ),
    )
    with patch.object(orchestrator_module, "Screener", _Screener):
        await orchestrator._analyse(
            result,
            snapshots=[],
            features={},
            stored={},
            client=cast(Any, object()),
            started=NOW,
        )
    return result


async def test_the_cycle_records_where_the_day_stood_before_and_after_it(
    settings: Settings,
) -> None:
    """$6.50 before, $10.50 after — one cycle, two sides of the limit.

    The before-reading is taken before the screener's own calls are committed, so
    it is genuinely "where the day stood when this cycle began".
    """
    store = CycleStore(spend_day=Decimal("6.50"))

    result = await _spend_states(store, settings)

    assert result.spend_state_before is SpendState.OK
    assert result.spend_state_after is SpendState.LIMIT_REACHED


async def test_a_cycle_that_changes_nothing_reports_the_same_state_twice(
    settings: Settings,
) -> None:
    """No transition, and therefore — by sentinel/bot/alerts.py — no message."""
    store = CycleStore(spend_day=Decimal("0"))

    result = await _spend_states(store, settings)

    assert result.spend_state_before is result.spend_state_after is SpendState.OK
