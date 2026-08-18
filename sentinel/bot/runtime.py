"""Runtime configuration set from Telegram (ARCHITECTURE.md §2, RISK_ENGINE.md §1).

"Precedence: DB > yaml > defaults." ``load_config`` has taken a ``db_overrides``
mapping since M0 and nothing ever supplied one, because the DB layer did not
exist. This module is that layer.

Three settings are owner-editable, and they are not all the same kind of thing:

* ``capital_eur`` is **not** in ``AppConfig`` at all — config.yaml deliberately
  omits it and the gate rejects with ``NO_CAPITAL`` until it is set. It belongs to
  ``AccountState``, so it is returned there rather than merged into the config.
* ``risk_per_trade_pct`` exists in both places. The gate reads it from
  ``AccountState``, so that is what ``/risk`` writes; the config value remains the
  default a fresh install starts from.
* ``watchlist`` is plain config, and is merged through ``load_config`` so the
  existing deep-merge decides precedence instead of a second mechanism.

Validation lives here rather than in the handlers: the bounds come from
``RiskConfig`` (0.25-1.5), and a handler that hardcoded them would drift from the
spec the moment config.yaml changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sentinel.core.config import AppConfig, RiskConfig, Settings, load_config
from sentinel.ingestion.models import InstrumentMeta
from sentinel.risk.models import AccountState

#: Keys in ``runtime_settings``. Strings, because they are stored in a database.
CAPITAL_EUR = "capital_eur"
RISK_PER_TRADE_PCT = "risk_per_trade_pct"
WATCHLIST = "watchlist"


@dataclass(frozen=True)
class Invalid:
    """A rejected value and the reason, phrased for the owner rather than a log."""

    message: str


def parse_capital(raw: str) -> Decimal | Invalid:
    """§3 — "Set capital EUR (validated > 0)".

    No upper bound is imposed: the spec sets none, and refusing a large number
    would be this module inventing a risk rule. A fat-fingered capital is visible
    on the very next card, which states it in full.
    """
    try:
        value = Decimal(raw.replace(",", "").replace("€", "").strip())
    except (InvalidOperation, ValueError):
        return Invalid(f"“{raw}” is not a number. Try: /capital 10000")
    # Decimal happily parses "nan" and "inf". Neither is a capital, and both would
    # poison every sizing calculation downstream if they reached AccountState.
    if not value.is_finite():
        return Invalid(f"“{raw}” is not a number. Try: /capital 10000")
    if value <= 0:
        return Invalid("Capital must be greater than zero.")
    return value


def parse_risk_pct(raw: str, risk: RiskConfig) -> Decimal | Invalid:
    """§3 — "Set per-trade risk % (0.25-1.5 enforced)", bounds read from config."""
    try:
        value = Decimal(raw.replace("%", "").strip())
    except (InvalidOperation, ValueError):
        return Invalid(f"“{raw}” is not a number. Try: /risk 0.75")
    if not value.is_finite():
        return Invalid(f"“{raw}” is not a number. Try: /risk 0.75")
    if value < risk.risk_per_trade_min_pct or value > risk.risk_per_trade_max_pct:
        return Invalid(
            f"Risk must be between {risk.risk_per_trade_min_pct}% and "
            f"{risk.risk_per_trade_max_pct}% (specs/RISK_ENGINE.md §1). "
            f"You asked for {value}%."
        )
    return value


def parse_symbol(raw: str) -> str | Invalid:
    """Normalize a watchlist symbol. Existence is checked against the exchange."""
    symbol = raw.strip().upper()
    if not symbol.isalnum() or not 5 <= len(symbol) <= 20:
        return Invalid(f"“{raw}” does not look like a symbol. Try: /watchlist add SOLUSDT")
    return symbol


class SymbolChecker(Protocol):
    """Just enough of ``MarketDataAdapter`` to confirm a symbol is real."""

    async def instrument_meta(self, symbol: str) -> InstrumentMeta: ...


async def verify_symbol(
    symbol: str, session_lookup: InstrumentMeta | None, checker: SymbolChecker | None
) -> Invalid | None:
    """Confirm a symbol exists before it joins the watchlist.

    The cached ``instrument_meta`` row answers for anything already ingested, which
    is every symbol the owner is likely to add. Only a genuinely new one costs a
    keyless public call. Skipping the check entirely would let one typo produce a
    silently skipped symbol on every cycle for as long as nobody reads the logs.

    ``None`` for ``checker`` means "cannot verify right now" — the symbol is
    accepted, and the caller says so, rather than blocking an edit on a network.
    """
    if session_lookup is not None or checker is None:
        return None
    try:
        await checker.instrument_meta(symbol)
    except Exception:
        return Invalid(
            f"{symbol} is not a Binance USDⓈ-M perpetual, or the exchange did not "
            "answer. Nothing was changed."
        )
    return None


def config_overrides(stored: dict[str, Any]) -> dict[str, Any]:
    """The ``db_overrides`` mapping for ``load_config`` — config keys only."""
    overrides: dict[str, Any] = {}
    if WATCHLIST in stored:
        overrides[WATCHLIST] = stored[WATCHLIST]
    return overrides


def effective_config(settings: Settings, stored: dict[str, Any]) -> AppConfig:
    """Re-resolve the config with DB overrides on top of yaml on top of defaults.

    ``AppConfig`` is frozen, so an override cannot be applied in place — the config
    is rebuilt from its sources instead. That is a feature: precedence stays in one
    tested function rather than being reimplemented as a set of attribute pokes.
    """
    overrides = config_overrides(stored)
    if not overrides:
        return settings.config
    return load_config(settings.secrets.config_path, db_overrides=overrides)


def account_state(stored: dict[str, Any], config: AppConfig, eurusd_rate: Decimal) -> AccountState:
    """Build the gate's ``AccountState`` from what the owner has set.

    ``capital_eur`` stays ``None`` when unset — the gate then rejects with
    ``NO_CAPITAL``, which is the correct, loud behaviour. Substituting a default
    would silently size positions against a number the owner never chose.
    """
    capital = stored.get(CAPITAL_EUR)
    risk_pct = stored.get(RISK_PER_TRADE_PCT)
    return AccountState(
        capital_eur=None if capital is None else Decimal(str(capital)),
        risk_per_trade_pct=(
            config.risk.risk_per_trade_pct if risk_pct is None else Decimal(str(risk_pct))
        ),
        eurusd_rate=eurusd_rate,
    )


def source_of(key: str, stored: dict[str, Any]) -> str:
    """``/settings`` labels every value with where it came from."""
    return "db" if key in stored else "yaml"


__all__ = [
    "CAPITAL_EUR",
    "RISK_PER_TRADE_PCT",
    "WATCHLIST",
    "Invalid",
    "account_state",
    "config_overrides",
    "effective_config",
    "parse_capital",
    "parse_risk_pct",
    "parse_symbol",
    "source_of",
    "verify_symbol",
]
