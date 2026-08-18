"""Cost estimation. Pure, `Decimal`, and honest about what it does not know.

CLAUDE.md requires a cost estimate on every logged LLM call. Rates are config
(`llm.pricing`), not code, so correcting a price never needs a release.
"""

from __future__ import annotations

from decimal import Decimal

from sentinel.core.config import ModelPricing
from sentinel.core.logging import get_logger
from sentinel.llm.models import TokenUsage

log = get_logger(__name__)

MILLION = Decimal("1000000")
#: Cents. Sub-cent precision is noise at these volumes, and a fixed quantum keeps
#: the stored Numeric stable across a re-computation.
QUANTUM = Decimal("0.000001")


def estimate_cost(
    model: str,
    usage: TokenUsage,
    pricing: dict[str, ModelPricing],
) -> Decimal:
    """USD for one call. An unpriced model costs 0 and says so — never a guess.

    Returning 0 rather than raising is deliberate: an unknown price must not lose
    the *call record*, which is the thing PRD G5 actually requires. The warning is
    what gets it fixed.
    """
    rates = pricing.get(model)
    if rates is None:
        log.warning(
            "llm.pricing_unknown",
            model=model,
            known_models=sorted(pricing),
            hint="add it under llm.pricing in config.yaml; cost logged as 0 until then",
        )
        return Decimal("0")

    total = (
        Decimal(usage.input_tokens) * rates.input_per_mtok
        + Decimal(usage.output_tokens) * rates.output_per_mtok
        + Decimal(usage.cache_read_tokens) * rates.cache_read_per_mtok
        + Decimal(usage.cache_write_tokens) * rates.cache_write_per_mtok
    ) / MILLION
    return total.quantize(QUANTUM)
