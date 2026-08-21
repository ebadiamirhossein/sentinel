"""Structured ingestion errors — every failure is typed, logged and degradable."""

from __future__ import annotations


class IngestionError(Exception):
    """Base class for every ingestion failure."""


class SourceUnavailable(IngestionError):
    """A data source failed after its retries. The caller degrades explicitly."""

    def __init__(self, source: str, reason: str, *, status_code: int | None = None) -> None:
        super().__init__(f"{source} unavailable: {reason}")
        self.source = source
        self.reason = reason
        #: The HTTP status when the failure was one, ``None`` for a transport error.
        #: Added in M10b so a caller can tell "the vendor is down, retry later" from
        #: "this credential is spent, a human must log in again" — two failures that
        #: look identical from the reason string and call for opposite responses
        #: (specs/FOREX.md §3.1).
        self.status_code = status_code


class CoreDataMissing(IngestionError):
    """Core OHLCV is missing or stale — the symbol is skipped this cycle (§4)."""

    def __init__(self, symbol: str, reason: str) -> None:
        super().__init__(f"{symbol} skipped: {reason}")
        self.symbol = symbol
        self.reason = reason
