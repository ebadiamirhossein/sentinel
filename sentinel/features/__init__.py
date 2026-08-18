"""Deterministic indicators, regimes and S/R detection (M2).

Pure functions. No LLM, no I/O — the values here must be identical every time the
same candles go in (CLAUDE.md), because the risk engine gates on ATR and the
chart renderer draws these levels.
"""

from sentinel.features.engine import attach, compute, compute_timeframe
from sentinel.features.models import (
    Level,
    LevelKind,
    RegimeBasis,
    SymbolFeatures,
    TimeframeFeatures,
    TrendRegime,
    VolatilityRegime,
)

__all__ = [
    "Level",
    "LevelKind",
    "RegimeBasis",
    "SymbolFeatures",
    "TimeframeFeatures",
    "TrendRegime",
    "VolatilityRegime",
    "attach",
    "compute",
    "compute_timeframe",
]
