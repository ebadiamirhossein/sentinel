"""Dark chart theme. Frozen constants — part of the reproducibility record.

Colours are chosen to survive the analyst's vision pipeline: high contrast
against the background, and the three EMAs distinguishable from each other and
from the candles without relying on red/green discrimination.
"""

from __future__ import annotations

from typing import Any, Final

BACKGROUND: Final = "#0d1117"
PANEL: Final = "#0d1117"
GRID: Final = "#21262d"
TEXT: Final = "#c9d1d9"
MUTED: Final = "#8b949e"

UP: Final = "#26a69a"
DOWN: Final = "#ef5350"
VOLUME_UP: Final = "#1b6b62"
VOLUME_DOWN: Final = "#8e3330"

#: EMA20 / EMA50 / EMA200 — distinct hues, none of them red or green.
EMA_COLOURS: Final[dict[int, str]] = {20: "#f0b90b", 50: "#5c9dff", 200: "#c77dff"}

SUPPORT: Final = "#26a69a"
RESISTANCE: Final = "#ef5350"
LEVEL_LABEL: Final = "#e6edf3"

#: Market-structure marks that are not S/R (M10b-2, FOREX.md §6). Distinct from
#: SUPPORT/RESISTANCE because they are a different kind of claim: an S/R level was
#: derived from touches, a prior-day high is simply where yesterday ended up.
REFERENCE: Final = "#e3b341"
REFERENCE_OPEN: Final = "#79c0ff"
#: The London-New York overlap, shaded behind the candles. Very low alpha: it is
#: context for the eye, and it must never compete with price.
SESSION_BAND: Final = "#58a6ff"
SESSION_BAND_ALPHA: Final = 0.07

#: Data-quality banner. Deliberately loud: the analyst is told to be stricter on
#: degraded data, so degradation has to be visible in the image, not only in the
#: JSON alongside it.
DEGRADED: Final = "#ff3b30"

FONT_FAMILY: Final = "DejaVu Sans"  # ships with matplotlib — no system-font dependency


def market_colors() -> dict[str, Any]:
    """mplfinance marketcolors kwargs."""
    return {
        "up": UP,
        "down": DOWN,
        "edge": {"up": UP, "down": DOWN},
        "wick": {"up": UP, "down": DOWN},
        "volume": {"up": VOLUME_UP, "down": VOLUME_DOWN},
    }


def style_rc() -> dict[str, Any]:
    """matplotlib rcParams applied to every render."""
    return {
        "font.family": FONT_FAMILY,
        "font.sans-serif": [FONT_FAMILY],
        # mplfinance's base styles ask for weight "medium", which DejaVu lacks;
        # matplotlib then falls back and warns on every render. Pin it.
        "font.weight": "normal",
        "axes.titleweight": "normal",
        "axes.labelweight": "normal",
        "text.color": TEXT,
        "axes.labelcolor": TEXT,
        "axes.edgecolor": GRID,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "grid.color": GRID,
        "figure.facecolor": BACKGROUND,
        "axes.facecolor": PANEL,
        "savefig.facecolor": BACKGROUND,
        "axes.grid": True,
        "grid.linestyle": ":",
        "grid.alpha": 0.45,
    }
