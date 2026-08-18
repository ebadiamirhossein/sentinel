"""Feature-engine contracts (Pydantic v2, frozen, Decimal-valued).

These are consumed by the screener (specs/PROMPTS.md §1), the analyst (§2) and
the risk engine (ATR, S/R distances). Values are ``Decimal`` so the risk engine
never has to convert a float into money math.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TrendRegime(StrEnum):
    """Owner-approved rule set — see :mod:`sentinel.features.regime`."""

    STRONG_UPTREND = "STRONG_UPTREND"
    UPTREND = "UPTREND"
    RANGE = "RANGE"
    DOWNTREND = "DOWNTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"
    UNKNOWN = "UNKNOWN"


class VolatilityRegime(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class RegimeBasis(StrEnum):
    """Which EMAs were available — recorded so a reduced read is never mistaken
    for a full one (e.g. the 1d tail is 100 candles, so EMA200 is absent)."""

    FULL = "FULL"  # EMA20/50/200 all present
    REDUCED = "REDUCED"  # EMA200 unavailable; classified on EMA20/50
    INSUFFICIENT = "INSUFFICIENT"


class LevelKind(StrEnum):
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"


class Level(Frozen):
    """A clustered pivot zone. Real structure only — never a round-number guess."""

    price: Decimal
    kind: LevelKind
    timeframe: str
    touches: int
    strength: Decimal
    first_touch_at: datetime
    last_touch_at: datetime
    #: Signed distance from the reference price, in percent (+ = above price).
    distance_pct: Decimal


class TimeframeFeatures(Frozen):
    """Everything computed from one timeframe's closed candles."""

    timeframe: str
    candles_used: int
    #: Close of the last **closed** candle (the in-progress bar is excluded).
    last_close: Decimal
    last_closed_at: datetime
    partial_candle_dropped: bool

    ema20: Decimal | None = None
    ema50: Decimal | None = None
    ema200: Decimal | None = None
    rsi14: Decimal | None = None
    atr14: Decimal | None = None
    atr_pct: Decimal | None = None
    relative_volume: Decimal | None = None
    pct_change_1: Decimal | None = None

    trend_regime: TrendRegime = TrendRegime.UNKNOWN
    regime_basis: RegimeBasis = RegimeBasis.INSUFFICIENT
    volatility_regime: VolatilityRegime = VolatilityRegime.UNKNOWN
    atr_pct_percentile: Decimal | None = None

    #: EMA relation flags for the screener block (specs/PROMPTS.md §1).
    price_above_ema20: bool | None = None
    price_above_ema50: bool | None = None
    price_above_ema200: bool | None = None
    ema_stack: str | None = None  # e.g. "20>50>200"


class SymbolFeatures(Frozen):
    """The feature block attached to a snapshot."""

    schema_version: int = 1
    symbol: str
    computed_at: datetime
    reference_price: Decimal

    timeframes: dict[str, TimeframeFeatures]
    levels: tuple[Level, ...] = ()

    #: 4h is the context/regime timeframe (PRD §7).
    htf_regime: TrendRegime = TrendRegime.UNKNOWN
    #: True when the 1h setup timeframe agrees in direction with the 4h context.
    regime_aligned: bool | None = None

    nearest_support: Decimal | None = None
    nearest_resistance: Decimal | None = None
    distance_to_support_pct: Decimal | None = None
    distance_to_resistance_pct: Decimal | None = None

    pct_change_1h: Decimal | None = None
    pct_change_4h: Decimal | None = None
    pct_change_24h: Decimal | None = None

    def screener_view(self) -> dict[str, Any]:
        """The compact numeric block the cheap screener consumes (PROMPTS.md §1)."""
        primary = self.timeframes.get("1h")
        context = self.timeframes.get("4h")
        return {
            "symbol": self.symbol,
            "last_price": str(self.reference_price),
            "pct_change_1h": _opt(self.pct_change_1h),
            "pct_change_4h": _opt(self.pct_change_4h),
            "pct_change_24h": _opt(self.pct_change_24h),
            "rsi_1h": _opt(primary.rsi14 if primary else None),
            "rsi_4h": _opt(context.rsi14 if context else None),
            "ema_stack_1h": primary.ema_stack if primary else None,
            "price_above_ema200_1h": primary.price_above_ema200 if primary else None,
            "relative_volume_1h": _opt(primary.relative_volume if primary else None),
            "atr_pct_1h": _opt(primary.atr_pct if primary else None),
            "trend_regime_1h": primary.trend_regime.value if primary else None,
            "trend_regime_4h": self.htf_regime.value,
            "regime_aligned": self.regime_aligned,
            "volatility_regime_1h": primary.volatility_regime.value if primary else None,
            "distance_to_support_pct": _opt(self.distance_to_support_pct),
            "distance_to_resistance_pct": _opt(self.distance_to_resistance_pct),
        }


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
