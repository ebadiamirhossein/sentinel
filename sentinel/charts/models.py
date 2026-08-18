"""Chart contracts: what was drawn, and everything needed to draw it again.

``ChartRenderParams`` is the reconstruction record. It is JSON-serialisable and
travels with the image; it is **not** stored in its own table — a chart has no
life independent of the signal it belongs to, so the params are embedded in the
signal record when that lands (M5/M6).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

#: Bumped whenever a change alters rendered pixels, so a stored chart's bytes
#: can be explained by the renderer that produced them.
RENDERER_VERSION = 1


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChartSpec(Frozen):
    """The inputs that decide what the picture looks like."""

    symbol: str
    timeframe: str
    #: Most recent N closed candles drawn. Legibility, not data availability:
    #: the feature engine still uses the full 200-candle tail.
    candle_window: int = 120
    width_px: int = 1600
    height_px: int = 1000
    dpi: int = 100
    volume_panel_ratio: float = 0.22
    ema_periods: tuple[int, ...] = (20, 50, 200)
    max_levels: int = 6

    @property
    def figsize(self) -> tuple[float, float]:
        """Inches. With dpi=100 these map 1:1 to the pixel dimensions above."""
        return (self.width_px / self.dpi, self.height_px / self.dpi)


class DrawnLevel(Frozen):
    """An S/R line actually drawn, recorded so the image can be reproduced."""

    price: Decimal
    kind: str
    touches: int
    timeframe: str


class ChartRenderParams(Frozen):
    """Everything needed to re-render this exact image."""

    renderer_version: int = RENDERER_VERSION
    spec: ChartSpec

    #: Provenance of the candles (specs/DATA_SOURCES.md §3).
    source: str
    fetched_at: datetime

    candles_drawn: int
    first_candle_at: datetime
    #: The last *closed* candle — what the watermark shows.
    last_candle_at: datetime
    last_close: Decimal

    emas_drawn: tuple[int, ...] = ()
    levels_drawn: tuple[DrawnLevel, ...] = ()

    data_quality: str = "OK"
    degraded_fields: tuple[str, ...] = ()

    #: sha256 of the PNG produced from these params.
    image_sha256: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ChartImage(Frozen):
    """A rendered PNG and its reconstruction record."""

    png: bytes
    params: ChartRenderParams

    @property
    def sha256(self) -> str:
        return self.params.image_sha256
