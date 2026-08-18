"""Structured ingestion errors — every failure is typed, logged and degradable."""

from __future__ import annotations


class IngestionError(Exception):
    """Base class for every ingestion failure."""


class SourceUnavailable(IngestionError):
    """A data source failed after its retries. The caller degrades explicitly."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source} unavailable: {reason}")
        self.source = source
        self.reason = reason


class CoreDataMissing(IngestionError):
    """Core OHLCV is missing or stale — the symbol is skipped this cycle (§4)."""

    def __init__(self, symbol: str, reason: str) -> None:
        super().__init__(f"{symbol} skipped: {reason}")
        self.symbol = symbol
        self.reason = reason
