"""The snapshot JSON the analyst sees, and the fence around its untrusted parts.

Two jobs:

1. **Select.** specs/PROMPTS.md §2 feeds the analyst the full ``MarketSnapshot``
   plus config limits. Raw candle arrays are excluded -- ~1,200 candles per
   symbol would be tens of thousands of tokens describing exactly what the three
   chart images already show. Everything that is *not* on a chart (features,
   derivatives, order-book imbalance, sentiment, macro, FX, staleness) is kept.

2. **Contain.** News headlines are third-party text: CryptoPanic and RSS
   republish whatever gets published. They are sanitized
   (``llm.untrusted``) and wrapped in one labelled ``<untrusted_news_data>``
   fence, and the prompt's HARD BOUNDARIES rule 8 tells the model what that fence
   means. See ``journal/PROMPT_LOG.md`` for the full rationale.

Key order is deterministic (``sort_keys`` at render time) so prompt-assembly
snapshot tests are stable and the cached prefix does not move between calls.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sentinel.core.config import RiskConfig
from sentinel.core.logging import get_logger
from sentinel.ingestion.models import MarketSnapshot, NewsContext
from sentinel.llm.untrusted import sanitize_untrusted, untrusted_section

log = get_logger(__name__)

#: Snapshot sections passed through as-is (minus news, which is fenced, and
#: ohlcv, which the charts carry).
PASSTHROUGH = ("instrument", "derivatives", "orderbook", "sentiment", "macro", "fx", "features")


def analyst_payload(snapshot: MarketSnapshot, limits: RiskConfig) -> dict[str, Any]:
    """The structured half of the analyst's user message (news excluded)."""
    dumped = snapshot.model_dump(mode="json", include=set(PASSTHROUGH))

    payload: dict[str, Any] = {
        "symbol": snapshot.symbol,
        "captured_at": snapshot.captured_at.isoformat(),
        "last_price": str(snapshot.last_price),
        "data_quality": snapshot.data_quality.value,
        "degraded_fields": list(snapshot.degraded_fields),
    }
    payload.update({key: dumped.get(key) for key in PASSTHROUGH})

    # The gate's thresholds, so the analyst knows what will be checked after it
    # (specs/PROMPTS.md §2 "Inputs"). These are limits, never sizing: no capital,
    # no leverage, no EUR reaches the analyst (ARCHITECTURE.md §4).
    payload["gate_limits"] = {
        "min_rr_to_tp1": str(limits.min_rr_tp1),
        "max_entry_distance_pct": str(limits.max_entry_distance_pct),
        "min_confidence_for_candidate": limits.min_confidence,
        "stop_atr_min_multiple": str(limits.stop_atr_min_multiple),
        "stop_atr_max_multiple": str(limits.stop_atr_max_multiple),
    }
    return payload


def news_block(news: NewsContext | None, *, symbol: str) -> str:
    """Sanitize every headline and wrap the lot in one labelled fence.

    Hostile text is defanged and **kept**, never dropped: the headline is
    evidence of an attempt, and M9 has to be able to see what was tried.
    """
    if news is None or not news.items:
        return untrusted_section([])

    lines: list[str] = []
    sanitized_count = 0
    for index, item in enumerate(news.items, start=1):
        title, title_changed = sanitize_untrusted(item.title)
        source, source_changed = sanitize_untrusted(item.source_domain)
        if title_changed or source_changed:
            sanitized_count += 1
            log.warning(
                "llm.untrusted_sanitized",
                symbol=symbol,
                provider=news.provider,
                source_domain=item.source_domain,
                original_title=item.title,
                sanitized_title=title,
            )
        marker = " [SANITIZED]" if title_changed or source_changed else ""
        lines.append(f"{index}. [{source} | {item.age_minutes}m ago]{marker} {title}")

    if sanitized_count:
        log.warning(
            "llm.untrusted_sanitized_batch",
            symbol=symbol,
            sanitized_items=sanitized_count,
            total_items=len(news.items),
        )
    return untrusted_section(lines)


def render_payload(payload: dict[str, Any]) -> str:
    """Stable JSON text. Sorted keys so the prompt bytes do not move."""
    return json.dumps(payload, indent=2, sort_keys=True, default=_default)


def _default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__} into the analyst payload")
