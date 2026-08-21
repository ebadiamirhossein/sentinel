"""Display helpers for the Telegram layer: escaping, time, and display **scale**.

**No arithmetic lives here.** ``tests/bot/test_no_arithmetic.py`` scans this
module and ``cards.py`` for arithmetic operators, because specs/TELEGRAM_UX.md §1
says every number on a card comes from ``TradePlan`` — the bot renders, it never
computes. A timezone conversion is a lookup, not a calculation: it re-labels one
instant, it does not produce a new quantity.

**M10c adds a third kind of lookup: display scale** (FOREX.md defect #22). A
``Decimal`` prints at whatever scale it carries, and a value read from a
``Numeric(38, 18)`` column carries eighteen — so the live card has been printing
``capital €200.000000000000000000``. Quantizing for display changes the *scale* and
never the *value*, which is why it belongs here and not in the engine: the two
fields affected are the only ones on ``TradePlan`` assigned without ``money()`` or
``percent()``, and the package that assigns them is frozen for the live measurement
window.

The rule this introduces, and the one the guard now enforces: **the renderer may fix
a scale; it may never change a value.** That rule's absence is why the defect lived —
``Decimal("200") == Decimal("200.000000000000000000")`` is true, so nothing that
compared values could see it, and nothing asserted on scale at all. It is the same
class as the ``$10`` -> ``$10.0`` regression ``tests/golden`` already names.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sentinel.core.logging import get_logger

log = get_logger(__name__)

#: specs/TELEGRAM_UX.md §6 — on every card, without exception.
DISCLAIMER = "Research tool — not financial advice. Past stats ≠ future results."

RULE = "──────────────"

#: Cents. Money is rendered at two decimals, always.
CENTS = Decimal("0.01")


def escape(text: str) -> str:
    """Escape the three characters Telegram's HTML parse mode reserves.

    Analyst prose reaches a card unchanged, and M5 §4 established that news text is
    attacker-influenceable. It is sanitized upstream before the model ever sees it;
    escaping again here means a thesis quoting a headline cannot break the markup
    even if that sanitizer is one day loosened.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money_eur(value: Decimal) -> str:
    """A euro amount at cents precision, for interpolation into a card.

    ``.quantize`` is a method call and survives the AST arithmetic scan for the same
    reason ``abs()`` does: it invents no magnitude. Trailing zeros are **kept** — money
    reads as money at two decimals, and "€200.0" is the exact regression the golden
    surfaces already carry a note about.
    """
    return f"{value.quantize(CENTS, rounding=ROUND_HALF_UP)}"


def percent_2dp(value: Decimal) -> str:
    """A **sizing** percentage at exactly two decimals — ``0.75``, ``1.50``, ``2.25``.

    Fixed rather than trimmed, deliberately, and the distinction is worth stating
    because this codebase contains both conventions and they are both right.

    ``sentinel.risk.rounding.percent`` **trims**, and every percentage the engine puts
    on a plan goes through it: a stop distance reads ``-1.78%`` and a liquidation
    distance reads ``20%``, because those are measurements and a trailing zero on a
    measurement is noise.

    These three are not measurements. ``risk per trade``, ``open risk`` and the rail
    they are compared against are **budget** figures, and they are already rendered at
    two decimals on the ``/status`` card. Padding keeps a risk of 1 reading as ``1.00%``
    beside ``0.75%`` instead of as a bare ``1%``, and — the reason it matters here —
    it changes not one byte of what those surfaces already print, so a fix for a
    *scale* defect does not smuggle a *convention* change in behind it.
    """
    return f"{value.quantize(CENTS, rounding=ROUND_HALF_UP)}"


def zone_info(name: str) -> ZoneInfo:
    """The owner's timezone, or UTC with a loud log if the name is unusable.

    A bad tz name in config must not stop a signal from being delivered — the card
    is the product. Falling back is safe precisely because the UTC figure is
    printed alongside the local one.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("bot.unknown_timezone", requested=name, using="UTC")
        return ZoneInfo("UTC")


def local_and_utc(moment: datetime, tz: ZoneInfo) -> str:
    """``2026-08-19 03:00 EEST (00:00 UTC)``.

    specs/DATA_SOURCES.md §4 puts the owner's timezone on rendered output and UTC
    everywhere internal. Both appear: local is what the owner acts on at 3am, and
    the UTC figure is what every log line and DB row carries, so a card can always
    be matched to its audit trail by eye.
    """
    local = moment.astimezone(tz)
    return f"{local:%Y-%m-%d %H:%M %Z} ({moment:%H:%M} UTC)"


def local_date_time(moment: datetime, tz: ZoneInfo) -> str:
    """Same as :func:`local_and_utc` but with the UTC date spelled out too."""
    local = moment.astimezone(tz)
    return f"{local:%Y-%m-%d %H:%M %Z} ({moment:%Y-%m-%d %H:%M} UTC)"


__all__ = [
    "CENTS",
    "DISCLAIMER",
    "RULE",
    "escape",
    "local_and_utc",
    "local_date_time",
    "money_eur",
    "percent_2dp",
    "zone_info",
]
