"""Cost estimation: exact Decimal arithmetic, and honest about unknown models."""

from __future__ import annotations

from decimal import Decimal

import structlog

from sentinel.core.config import AppConfig
from sentinel.llm.models import TokenUsage
from sentinel.llm.pricing import estimate_cost


def test_unknown_model_costs_zero_and_warns(app_config: AppConfig) -> None:
    """A missing price must never become a guessed price."""
    usage = TokenUsage(input_tokens=1000, output_tokens=100)
    with structlog.testing.capture_logs() as logs:
        cost = estimate_cost("claude-not-a-real-model", usage, app_config.llm.pricing)

    assert cost == Decimal("0")
    assert any(line["event"] == "llm.pricing_unknown" for line in logs)


def test_cost_includes_cache_tiers(app_config: AppConfig) -> None:
    usage = TokenUsage(
        input_tokens=1000, output_tokens=1000, cache_read_tokens=10000, cache_write_tokens=1000
    )
    # (1000*10 + 1000*50 + 10000*1 + 1000*12.5) / 1e6
    assert estimate_cost("claude-fable-5", usage, app_config.llm.pricing) == Decimal("0.082500")


def test_zero_usage_costs_nothing(app_config: AppConfig) -> None:
    assert estimate_cost("claude-fable-5", TokenUsage(), app_config.llm.pricing) == Decimal("0")


def test_cost_is_exact_not_float(app_config: AppConfig) -> None:
    """Money math is Decimal end to end (CLAUDE.md) -- no float artefact anywhere."""
    usage = TokenUsage(input_tokens=333, output_tokens=777)
    cost = estimate_cost("claude-sonnet-4-6", usage, app_config.llm.pricing)
    assert isinstance(cost, Decimal)
    assert cost == Decimal("0.012654")
