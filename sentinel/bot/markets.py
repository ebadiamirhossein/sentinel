"""Which market(s) a command is about (M10a Step 7).

Every user-facing surface that shows market-specific data has to answer two
questions: which markets did the caller ask for, and does the answer need to say
so? This module answers both in one place, because the alternative is six handlers
each deciding it slightly differently.

**The default, documented once here and in specs/TELEGRAM_UX.md §3e:** a command
with no market argument covers **every enabled market, in the order config.yaml
names them**. Not "the first one" and not "crypto": a reader who types ``/stats``
and is shown one market's numbers, with nothing saying the other exists, has been
told something false by omission — and that is precisely the failure mode this
project keeps meeting.

**And the rule that keeps today's output intact:** with one market enabled,
:func:`section_header` returns nothing at all, so every card renders exactly the
text it rendered before this milestone. The condition is ``AppConfig.multi_market``,
in one place, rather than six renderers each remembering to check.
"""

from __future__ import annotations

from sentinel.bot.runtime import Invalid
from sentinel.core.config import AppConfig
from sentinel.core.markets import Market


def parse_market(raw: str) -> Market | None:
    """A market named by a user, or ``None`` if this is not a market name.

    ``None`` rather than an error, because every caller is parsing an argument that
    might be something else entirely — ``/stats 30d``, ``/pulse SOLUSDT`` — and
    "not a market" is a normal answer there, not a mistake to report.
    """
    try:
        return Market(raw.strip().lower())
    except ValueError:
        return None


def resolve_markets(raw: str | None, config: AppConfig) -> tuple[Market, ...] | Invalid:
    """The markets a command should cover, given its optional argument.

    ``None`` or an unrecognised word means every enabled market. A named market that
    is **disabled** is an error rather than an empty answer: somebody asking for
    forex today should be told it is switched off, not handed a blank card they
    would read as "no signals yet".
    """
    enabled = config.enabled_markets
    if raw is None:
        return enabled
    named = parse_market(raw)
    if named is None:
        return enabled
    if named not in enabled:
        available = ", ".join(market.value for market in enabled) or "none"
        return Invalid(
            f"<b>{named.value}</b> is not enabled on this deployment. Available: {available}."
        )
    return (named,)


def market_of_symbol(symbol: str, config: AppConfig) -> Market:
    """Which enabled market watches this symbol.

    Used by the symbol-shaped commands — ``/pulse SOLUSDT``, ``/snapshot EURUSD`` —
    so a reader never has to name a market the symbol already implies. Symbols are
    disjoint across markets, which is what makes this answerable at all; the same
    assumption the ``ohlcv_candles`` primary key rests on, and it is recorded in
    journal/M10a_REPORT.md as something M10b should stop relying on.

    Falls back to the first enabled market when nothing watches the symbol. The
    caller is then rendering a "not on the watchlist" card, whose whole content is
    that there is no data — so which market it asked is immaterial, and refusing
    would replace a helpful answer with an error about a distinction the reader has
    not been shown.
    """
    upper = symbol.upper()
    for market in config.enabled_markets:
        if upper in config.market(market).watchlist:
            return market
    enabled = config.enabled_markets
    return enabled[0] if enabled else Market.CRYPTO


def section_header(market: Market, config: AppConfig) -> str:
    """The line naming a market above its section, or ``""`` with only one enabled.

    The whole of M10a's "do not change crypto's visual output beyond adding a market
    tag, and put the tag behind the multi-market condition". One function, so the
    golden surfaces have one thing to prove and M10b has one thing to change.
    """
    if not config.multi_market:
        return ""
    return f"<b>— {market.value.upper()} —</b>"


__all__ = ["market_of_symbol", "parse_market", "resolve_markets", "section_header"]
