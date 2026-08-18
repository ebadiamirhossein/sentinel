"""The compact numeric block the cheap tier sees. Pure -- no LLM, no network.

specs/PROMPTS.md §1 lists the fields exactly: last price, % changes, RSI, EMA
relation flags, relative volume, ATR%, funding, OI 24h change, F&G, distance to
nearest S/R, and a news *flag*.

Note what is **not** here: headline text. §1 specifies ``news_flag`` -- a count --
so no third-party free text reaches the screener at all. That is a real security
property, not an accident, and ``tests/screener/test_payload.py`` asserts it so
it stays true if someone later thinks headlines would help triage.

Missing sources produce ``null``. Nothing is ever substituted
(specs/DATA_SOURCES.md §4).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import MarketSnapshot

#: specs/DATA_SOURCES.md §4 / PROMPTS §1: "fresh" news means inside 6 hours.
NEWS_FRESHNESS_MINUTES = 360


def screener_block(snapshot: MarketSnapshot, features: SymbolFeatures) -> dict[str, Any]:
    """One symbol's triage row."""
    block = dict(features.screener_view())
    block["funding_rate"] = _opt(_funding(snapshot))
    block["oi_change_24h_pct"] = _opt(_oi_change_pct(snapshot))
    block["fear_greed"] = snapshot.sentiment.value if snapshot.sentiment else None
    block["news_flag"] = _news_flag(snapshot)
    block["data_quality"] = snapshot.data_quality.value
    return block


def _funding(snapshot: MarketSnapshot) -> Decimal | None:
    return snapshot.derivatives.funding_rate if snapshot.derivatives else None


def _oi_change_pct(snapshot: MarketSnapshot) -> Decimal | None:
    """Percent change across the stored 24h open-interest series.

    Returns ``None`` rather than 0 when the series is absent or its first point
    is zero -- an unknown change and a flat one are different facts.
    """
    derivatives = snapshot.derivatives
    if derivatives is None or len(derivatives.open_interest_24h) < 2:
        return None
    first = derivatives.open_interest_24h[0].open_interest_base
    last = derivatives.open_interest_24h[-1].open_interest_base
    if first == 0:
        return None
    return ((last - first) / first) * Decimal(100)


def _news_flag(snapshot: MarketSnapshot) -> int:
    """Count of fresh headlines. A count, deliberately -- never the text."""
    if snapshot.news is None:
        return 0
    return sum(1 for item in snapshot.news.items if item.age_minutes < NEWS_FRESHNESS_MINUTES)


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
