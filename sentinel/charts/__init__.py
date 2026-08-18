"""mplfinance chart rendering to PNG for analyst vision input (M3).

Deterministic: identical OHLCV produces a byte-identical PNG, so any stored
signal's charts can be reproduced later (PRD F4).
"""

from sentinel.charts.models import ChartImage, ChartRenderParams, ChartSpec, DrawnLevel
from sentinel.charts.renderer import render, render_album

__all__ = [
    "ChartImage",
    "ChartRenderParams",
    "ChartSpec",
    "DrawnLevel",
    "render",
    "render_album",
]
