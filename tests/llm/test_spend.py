"""The LLM spend guard (M7, pulled forward from M8's "spend guard" item).

Why this exists at M7 rather than M8: M7 is the first milestone where the
scheduler runs unattended. A bug, or a market event that makes every symbol look
interesting, can spend real money between midnight and breakfast — and the
cheapest moment to build the brake is before the first unattended night, not
after it.

What it gates is deliberately narrow. Reaching the daily limit suspends **new
deep analysis**. The screener keeps triaging, and the tracker keeps managing open
positions — a guard that could stop the tracker would be a guard that abandons a
live position to protect a dollar.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.core.config import AppConfig, load_config
from sentinel.llm.spend import SpendState, SpendTotals, evaluate_spend, spend_window

NOW = datetime(2026, 8, 18, 13, 30, tzinfo=UTC)


@pytest.fixture
def config() -> AppConfig:
    return load_config()


def totals(*, day: str = "0", month: str = "0", calls: int = 0, unpriced: int = 0) -> SpendTotals:
    return SpendTotals(
        day_usd=Decimal(day), month_usd=Decimal(month), calls=calls, unpriced_calls=unpriced
    )


# --------------------------------------------------------------------------- #
# The window the accumulators cover
# --------------------------------------------------------------------------- #


def test_the_day_starts_at_midnight_utc() -> None:
    """Owner ruling: the UTC calendar day, matching every stored row and log line,
    so a suspension is always reconcilable against the audit trail by eye."""
    day_start, month_start = spend_window(NOW)
    assert day_start == datetime(2026, 8, 18, 0, 0, tzinfo=UTC)
    assert month_start == datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def test_a_call_one_second_before_midnight_belongs_to_the_previous_day() -> None:
    just_before = datetime(2026, 8, 18, 23, 59, 59, tzinfo=UTC)
    just_after = datetime(2026, 8, 19, 0, 0, 0, tzinfo=UTC)
    assert spend_window(just_before)[0] == datetime(2026, 8, 18, tzinfo=UTC)
    assert spend_window(just_after)[0] == datetime(2026, 8, 19, tzinfo=UTC)


def test_the_month_rolls_on_the_first() -> None:
    assert spend_window(datetime(2026, 9, 1, 0, 30, tzinfo=UTC))[1] == datetime(
        2026, 9, 1, tzinfo=UTC
    )
    assert spend_window(datetime(2026, 12, 31, 23, 0, tzinfo=UTC))[1] == datetime(
        2026, 12, 1, tzinfo=UTC
    )


def test_the_window_refuses_a_naive_instant() -> None:
    """Everything internal is UTC (CLAUDE.md). A naive datetime here would silently
    put the day boundary wherever the host's clock happens to sit."""
    with pytest.raises(ValueError):
        spend_window(datetime(2026, 8, 18, 13, 30))  # noqa: DTZ001


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #


def test_a_quiet_day_is_ok(config: AppConfig) -> None:
    assert evaluate_spend(totals(day="1.20"), config.llm) is SpendState.OK


def test_crossing_the_warn_level_warns_without_suspending(config: AppConfig) -> None:
    assert config.llm.daily_spend_warn_usd == Decimal("7")
    assert evaluate_spend(totals(day="7.00"), config.llm) is SpendState.WARN
    assert evaluate_spend(totals(day="9.99"), config.llm) is SpendState.WARN


def test_reaching_the_limit_suspends(config: AppConfig) -> None:
    """``>=``, not ``>``: $10.00 spent against a $10 limit is the limit reached.
    Waiting for a cent more would let a run of cheap calls sit at the ceiling."""
    assert config.llm.daily_spend_limit_usd == Decimal("10")
    assert evaluate_spend(totals(day="10.00"), config.llm) is SpendState.LIMIT_REACHED
    assert evaluate_spend(totals(day="41.00"), config.llm) is SpendState.LIMIT_REACHED


def test_the_limit_outranks_the_warning(config: AppConfig) -> None:
    """A day past the limit is not merely a warning, whatever order the thresholds
    happen to be configured in."""
    assert evaluate_spend(totals(day="12"), config.llm) is SpendState.LIMIT_REACHED


def test_a_warn_level_above_the_limit_never_masks_the_limit(config: AppConfig) -> None:
    """A misconfiguration must fail safe. If someone sets warn above limit, the
    limit still trips first."""
    inverted = config.llm.model_copy(
        update={"daily_spend_warn_usd": Decimal("20"), "daily_spend_limit_usd": Decimal("10")}
    )
    assert evaluate_spend(totals(day="11"), inverted) is SpendState.LIMIT_REACHED


def test_only_the_day_gates_spending(config: AppConfig) -> None:
    """Monthly spend is accumulated and reported, never gated: a monthly cap that
    tripped on the 28th would silence the system for three days, and the daily
    limit already bounds the month at 31x."""
    assert evaluate_spend(totals(day="0.50", month="900"), config.llm) is SpendState.OK


# --------------------------------------------------------------------------- #
# Unpriced calls — the hole a spend guard must not have
# --------------------------------------------------------------------------- #


def test_an_unpriced_call_makes_the_figure_a_floor_rather_than_a_total() -> None:
    """``pricing.estimate_cost`` prices an unknown model at **0** with a warning
    (M5 decision) — correct for keeping the audit row, and a hole in a spend
    guard: an unpriced model would spend invisibly. The totals therefore carry the
    count, and everything that renders the figure says "at least"."""
    assert totals(day="4.00", unpriced=3).is_floor is True
    assert totals(day="4.00").is_floor is False


def test_an_unpriced_call_never_counts_as_free(config: AppConfig) -> None:
    """It cannot be priced, so it cannot be added — but it must not be reported as
    a completed, costless call either. The count is the honest degradation
    (DATA_SOURCES §4), exactly as ``funding n/a`` is on a card."""
    state = totals(day="4.00", calls=12, unpriced=3)
    assert state.priced_calls == 9
    assert evaluate_spend(state, config.llm) is SpendState.OK


# --------------------------------------------------------------------------- #
# The tracker must never be able to spend, at all
# --------------------------------------------------------------------------- #


def test_nothing_in_the_tracker_can_reach_an_llm() -> None:
    """Structural, not behavioural: the guard suspends deep analysis, and it is
    only safe to let it do that because the tracker cannot make an LLM call in the
    first place.

    CLAUDE.md forbids an LLM in the deterministic modules; this asserts it for the
    one module whose independence the spend guard actively relies on. An import
    scan rather than a mock, because a mock only proves the path a test happened
    to take.
    """
    tracker = Path(__file__).resolve().parents[2] / "sentinel" / "tracker"
    offenders: list[str] = []
    for path in sorted(tracker.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module.startswith(("sentinel.llm", "sentinel.analyst.providers", "anthropic")):
                    offenders.append(f"{path.name}:{node.lineno} imports {module}")

    assert not offenders, (
        "the tracker must run without an LLM — it manages open positions and keeps "
        f"running when the spend guard suspends analysis: {offenders}"
    )
