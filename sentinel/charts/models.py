"""Chart contracts: what was drawn, and everything needed to draw it again.

``ChartRenderParams`` is the reconstruction record. It is JSON-serialisable and
travels with the image; it is **not** stored in its own table — a chart has no
life independent of the signal it belongs to, so the params are embedded in the
signal record when that lands (M5/M6).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

#: Bumped whenever a change alters rendered pixels, so a stored chart's bytes
#: can be explained by the renderer that produced them.
RENDERER_VERSION = 1


if TYPE_CHECKING:  # pragma: no cover - import cycle: config imports nothing from here
    from sentinel.core.config import ChartsConfig


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


def album_specs(charts: ChartsConfig, symbol: str) -> tuple[ChartSpec, ...]:
    """The analyst's chart album for one symbol, straight from config (PRD F4).

    One definition, because there are now three callers — the crypto cycle, the forex
    cycle and ``sentinel.tools.forex_prompt_cost`` — and a second copy would measure a
    *different* album than the one the pipeline sends. The token count that sets a spend
    rail has to be the count of the real payload.
    """
    return tuple(
        ChartSpec(
            symbol=symbol,
            timeframe=timeframe,
            candle_window=charts.candle_window,
            width_px=charts.width_px,
            height_px=charts.height_px,
            dpi=charts.dpi,
            volume_panel_ratio=charts.volume_panel_ratio,
            ema_periods=charts.ema_periods,
            max_levels=charts.max_levels,
        )
        for timeframe in charts.timeframes
    )


class DrawnLevel(Frozen):
    """An S/R line actually drawn, recorded so the image can be reproduced."""

    price: Decimal
    kind: str
    touches: int
    timeframe: str


class AnnotationKind(StrEnum):
    """Market-structure marks that are not S/R levels (FOREX.md §6).

    Deliberately not folded into :class:`DrawnLevel`: that model carries a ``touches``
    count, and a prior-day high has no touch count. Putting a zero there would be the
    fabricated number §2.1 exists to forbid, and the same defect class as trying to
    carry a forex plan in a crypto ``TradePlan`` (spec defect #12).
    """

    PRIOR_DAY_HIGH = "prior_day_high"
    PRIOR_DAY_LOW = "prior_day_low"
    PRIOR_WEEK_HIGH = "prior_week_high"
    PRIOR_WEEK_LOW = "prior_week_low"
    DAILY_OPEN = "daily_open"
    WEEKLY_OPEN = "weekly_open"
    SESSION_BAND = "session_band"

    @property
    def is_band(self) -> bool:
        return self is AnnotationKind.SESSION_BAND


class ChartAnnotation(Frozen):
    """One mark on the chart: a horizontal reference line, or a shaded time band.

    A line carries a price and no interval; a band carries an interval and no price.
    Neither ever carries a zero for the other — the validator below makes that
    unrepresentable rather than merely discouraged.

    This is both the **input** to :func:`sentinel.charts.renderer.render` and the
    **record** of what it drew, which is what keeps the two from drifting: the params
    report the annotations actually rendered, not the ones that were requested.
    """

    kind: AnnotationKind
    label: str
    price: Decimal | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None

    @model_validator(mode="after")
    def _carries_exactly_what_its_kind_has(self) -> ChartAnnotation:
        if self.kind.is_band:
            if self.price is not None:
                raise ValueError(f"{self.kind.value} is a time band and has no price")
            if self.from_at is None or self.to_at is None:
                raise ValueError(f"{self.kind.value} needs both ends of its interval")
            if self.to_at <= self.from_at:
                raise ValueError(f"{self.kind.value} ends at or before it starts")
        else:
            if self.price is None:
                raise ValueError(f"{self.kind.value} is a price line and needs a price")
            if self.from_at is not None or self.to_at is not None:
                raise ValueError(f"{self.kind.value} is a price line and has no interval")
        return self


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

    #: Market-structure marks actually drawn (M10b-2). Empty for crypto, which has
    #: none of them — and when it is empty the key is **omitted entirely** by the
    #: serializer below rather than rendered as ``[]``.
    annotations: tuple[ChartAnnotation, ...] = ()

    data_quality: str = "OK"
    degraded_fields: tuple[str, ...] = ()

    #: sha256 of the PNG produced from these params.
    image_sha256: str = ""

    @model_serializer(mode="wrap")
    def _omit_empty_annotations(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Serialise ``annotations`` only when there are some (M10b-2, owner ruling G1).

        The reconstruction record has to be complete — a chart that draws a line
        nothing in the database explains breaks the property M3 exists for. But this
        model is dumped whole into the crypto chart golden, and a new key would move
        bytes for a market that has no annotations at all.

        Both hold if the field is conditional: crypto's ``to_json_dict()`` is
        byte-identical to what it was before this field existed, and forex's carries
        every line and band it drew. ``mode="wrap"`` rather than editing
        :meth:`to_json_dict`, so **every** serialisation path is covered — including a
        nested dump of the enclosing :class:`ChartImage`, which is not one anybody
        writes today and is exactly the sort that appears later.
        """
        data: dict[str, Any] = handler(self)
        if not self.annotations:
            data.pop("annotations", None)
        return data

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ChartImage(Frozen):
    """A rendered PNG and its reconstruction record."""

    png: bytes
    params: ChartRenderParams

    @property
    def sha256(self) -> str:
        return self.params.image_sha256
