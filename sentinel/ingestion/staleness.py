"""Data-quality rules — specs/DATA_SOURCES.md §4. Pure functions, no I/O.

    - Each field carries a max_age: OHLCV 2x its timeframe, funding 15m, OI 30m,
      F&G 24h, news 6h window.
    - Any core field (OHLCV) stale or missing  → skip the symbol.
    - Any secondary field stale                → DEGRADED + the list of degraded
                                                 fields, passed on to the analyst.

Nothing here substitutes a value: a missing field stays missing and is named.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sentinel.core.config import MaxAgeConfig
from sentinel.ingestion.models import (
    BookSnapshot,
    DerivContext,
    FxRate,
    MacroContext,
    NewsContext,
    OHLCVSeries,
    SentimentContext,
    Stamped,
)

#: Minutes per supported timeframe string.
TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "12h": 720,
    "1d": 1440,
    "1w": 10_080,
}


def timeframe_to_timedelta(timeframe: str) -> timedelta:
    try:
        return timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe {timeframe!r}") from exc


def is_stale(fetched_at: datetime, now: datetime, max_age: timedelta) -> bool:
    return (now - fetched_at) > max_age


def ohlcv_max_age(timeframe: str, multiplier: int) -> timedelta:
    """§4: OHLCV tolerates 2x its own timeframe."""
    return timeframe_to_timedelta(timeframe) * multiplier


@dataclass(frozen=True)
class QualityReport:
    """Outcome of evaluating one symbol's snapshot parts."""

    degraded_fields: tuple[str, ...] = ()
    #: Set when a *core* field is stale/missing — the caller skips the symbol.
    skip_reason: str | None = None

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded_fields)


@dataclass
class SnapshotParts:
    """What the assembler managed to collect for one symbol."""

    ohlcv: dict[str, OHLCVSeries] = field(default_factory=dict)
    derivatives: DerivContext | None = None
    orderbook: BookSnapshot | None = None
    news: NewsContext | None = None
    sentiment: SentimentContext | None = None
    macro: MacroContext | None = None
    fx: FxRate | None = None
    #: Sources that raised while being fetched, e.g. {"funding": "HTTP 503"}.
    failures: dict[str, str] = field(default_factory=dict)


def evaluate(
    parts: SnapshotParts,
    *,
    now: datetime,
    required_timeframes: tuple[str, ...],
    max_age: MaxAgeConfig,
) -> QualityReport:
    """Classify a symbol's data: skip, DEGRADED, or OK."""
    degraded: list[str] = []

    # ── core: OHLCV ──────────────────────────────────────────────────────────
    for timeframe in required_timeframes:
        series = parts.ohlcv.get(timeframe)
        if series is None:
            return QualityReport(skip_reason=f"missing OHLCV {timeframe}")
        if not series.candles:
            return QualityReport(skip_reason=f"empty OHLCV {timeframe}")
        if is_stale(series.fetched_at, now, ohlcv_max_age(timeframe, max_age.ohlcv_multiplier)):
            return QualityReport(skip_reason=f"stale OHLCV {timeframe}")

    # ── secondary fields: degrade, never substitute ──────────────────────────
    def check(name: str, stamped: Stamped | None, budget_seconds: int) -> None:
        if stamped is None:
            degraded.append(name)
            return
        if is_stale(stamped.fetched_at, now, timedelta(seconds=budget_seconds)):
            degraded.append(name)

    if parts.derivatives is None:
        degraded.extend(("funding", "open_interest", "long_short_ratio"))
    else:
        deriv = parts.derivatives
        check("funding", deriv, max_age.funding)
        check("open_interest", deriv, max_age.open_interest)
        if deriv.long_short_ratio is None:
            degraded.append("long_short_ratio")
        else:
            check("long_short_ratio", deriv, max_age.long_short_ratio)

    check("orderbook", parts.orderbook, max_age.orderbook)
    check("fear_greed", parts.sentiment, max_age.fear_greed)
    check("btc_dominance", parts.macro, max_age.btc_dominance)
    check("news", parts.news, max_age.news_window)
    check("eurusd", parts.fx, max_age.eurusd)

    if parts.fx is not None and parts.fx.is_last_known_good and "eurusd" not in degraded:
        degraded.append("eurusd")

    # Dedupe while preserving order, so the analyst sees a stable list.
    seen: dict[str, None] = {}
    for name in degraded:
        seen.setdefault(name, None)
    return QualityReport(degraded_fields=tuple(seen))
