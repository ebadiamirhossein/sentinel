"""Display helpers for the Telegram layer: escaping, and time.

**No arithmetic lives here.** ``tests/bot/test_no_arithmetic.py`` scans this
module and ``cards.py`` for arithmetic operators, because specs/TELEGRAM_UX.md §1
says every number on a card comes from ``TradePlan`` — the bot renders, it never
computes. A timezone conversion is a lookup, not a calculation: it re-labels one
instant, it does not produce a new quantity.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sentinel.core.logging import get_logger

log = get_logger(__name__)

#: specs/TELEGRAM_UX.md §6 — on every card, without exception.
DISCLAIMER = "Research tool — not financial advice. Past stats ≠ future results."

RULE = "──────────────"


def escape(text: str) -> str:
    """Escape the three characters Telegram's HTML parse mode reserves.

    Analyst prose reaches a card unchanged, and M5 §4 established that news text is
    attacker-influenceable. It is sanitized upstream before the model ever sees it;
    escaping again here means a thesis quoting a headline cannot break the markup
    even if that sanitizer is one day loosened.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


__all__ = ["DISCLAIMER", "RULE", "escape", "local_and_utc", "local_date_time", "zone_info"]
