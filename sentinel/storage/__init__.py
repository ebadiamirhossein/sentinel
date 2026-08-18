"""SQLAlchemy models, repositories and the async engine.

Dependency direction (CLAUDE.md): every pipeline stage may import ``storage``;
``storage`` imports nothing from them.
"""

from sentinel.storage.base import Base
from sentinel.storage.db import Database
from sentinel.storage.models import (
    FxRateRow,
    IngestionFailureRow,
    InstrumentMetaRow,
    MarketSnapshotRow,
    OhlcvCandleRow,
)
from sentinel.storage.repositories import (
    FxRateRepository,
    IngestionFailureRepository,
    InstrumentMetaRepository,
    SnapshotRepository,
)

__all__ = [
    "Base",
    "Database",
    "FxRateRepository",
    "FxRateRow",
    "IngestionFailureRepository",
    "IngestionFailureRow",
    "InstrumentMetaRepository",
    "InstrumentMetaRow",
    "MarketSnapshotRow",
    "OhlcvCandleRow",
    "SnapshotRepository",
]
