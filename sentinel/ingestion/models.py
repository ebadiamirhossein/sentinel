"""Cross-module contracts for ingestion (ARCHITECTURE.md §3, contract 1).

Every record carries ``source`` + ``fetched_at`` (specs/DATA_SOURCES.md §3), so
staleness is computable from the snapshot alone and nothing is ever silently
substituted. Prices and volumes are ``Decimal`` — floats only appear at the
pandas boundary (:meth:`OHLCVSeries.to_frame`), which the feature engine needs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from sentinel.core.markets import LEGACY_MARKET, Market

if TYPE_CHECKING:  # pragma: no cover — import cost lands on the feature engine
    import pandas as pd


def _to_decimal(value: Any) -> Any:
    """Exchange payloads arrive as float/str; convert through ``str``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    return value


Money = Annotated[Decimal, BeforeValidator(_to_decimal)]


class DataQuality(StrEnum):
    """specs/DATA_SOURCES.md §4."""

    OK = "OK"
    DEGRADED = "DEGRADED"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Stamped(Frozen):
    """Provenance every fetched record must carry."""

    source: str
    fetched_at: datetime


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


class Candle(Frozen):
    """One bar. ``volume`` is optional **because forex has none** (M10b).

    specs/FOREX.md §2.1: where an input does not exist the feature is absent — not
    zero, not a placeholder. Saxo publishes no volume field of any kind, not even
    tick counts, and a zero standing in for "no data" reads to a model as "no
    activity" and yields a confident answer built on nothing.

    Optional is therefore right, and it is also dangerous: "nullable" quietly
    becoming "sometimes missing" for **crypto** would corrupt relative volume with
    nothing to notice it by. So the permission is granted per market and policed —
    see :func:`assert_volume_matches_market`, which :class:`OHLCVSeries` applies on
    construction so a mismatched candle cannot exist in the first place.
    """

    open_time: datetime
    open: Money
    high: Money
    low: Money
    close: Money
    volume: Money | None = None


def assert_volume_matches_market(candles: Sequence[Candle], market: Market) -> None:
    """Volume is required for crypto and forbidden for forex (M10b, owner R-b).

    Both directions, deliberately. Only checking the forex side would let a crypto
    candle arrive with a null volume and silently disable relative volume; only
    checking the crypto side would let a forex adapter invent a zero, which is the
    exact substitution §2.1 forbids.
    """
    for candle in candles:
        if market is Market.FOREX:
            if candle.volume is not None:
                raise ValueError(
                    f"forex candle at {candle.open_time.isoformat()} carries volume "
                    f"{candle.volume} — this market has no volume of any kind, and a "
                    f"number here would be a fabricated one (specs/FOREX.md §2.1)"
                )
        elif candle.volume is None:
            raise ValueError(
                f"{market.value} candle at {candle.open_time.isoformat()} has no volume — "
                f"volume is optional only because forex has none, never because a "
                f"{market.value} fetch came back short"
            )


class OHLCVSeries(Stamped):
    symbol: str
    timeframe: str
    candles: tuple[Candle, ...]
    #: Which market these candles belong to (M10b). Carried on the series rather
    #: than passed alongside it, so the volume invariant above can be enforced at
    #: construction and there is no path that builds a series without deciding.
    market: Market = LEGACY_MARKET

    @model_validator(mode="after")
    def _volume_matches_market(self) -> OHLCVSeries:
        assert_volume_matches_market(self.candles, self.market)
        return self

    @property
    def last(self) -> Candle:
        return self.candles[-1]

    @property
    def last_close(self) -> Decimal:
        return self.candles[-1].close

    def to_frame(self) -> pd.DataFrame:
        """DataFrame view for the feature engine (M2) and chart renderer (M3).

        The Decimal→float conversion happens here and only here: indicator math
        needs float64, money math upstream does not.
        """
        import pandas as pd

        frame = pd.DataFrame(
            {
                "open_time": [c.open_time for c in self.candles],
                "open": [float(c.open) for c in self.candles],
                "high": [float(c.high) for c in self.candles],
                "low": [float(c.low) for c in self.candles],
                "close": [float(c.close) for c in self.candles],
                # NaN, never 0.0, when the market has no volume: a zero would be
                # indistinguishable from a genuinely silent bar to every indicator
                # downstream (specs/FOREX.md §2.1).
                "volume": [
                    float("nan") if c.volume is None else float(c.volume) for c in self.candles
                ],
            }
        )
        return frame.set_index("open_time")


class OpenInterestPoint(Frozen):
    at: datetime
    open_interest_base: Money
    open_interest_value: Money | None = None


class DerivContext(Stamped):
    """Funding, open interest and top-trader positioning (§2.1).

    ``next_funding_rate`` is optional: Binance's premiumIndex publishes the next
    settlement *time* but not a predicted next rate, so ccxt returns None.
    """

    funding_rate: Money
    next_funding_time: datetime | None = None
    next_funding_rate: Money | None = None
    mark_price: Money | None = None
    open_interest_base: Money
    open_interest_value: Money | None = None
    open_interest_24h: tuple[OpenInterestPoint, ...] = ()
    long_short_ratio: Money | None = None
    long_account_pct: Money | None = None
    short_account_pct: Money | None = None


class BookSnapshot(Stamped):
    """Top-N depth reduced to deterministic features. The raw book never reaches the LLM."""

    depth_levels: int
    best_bid: Money
    best_ask: Money
    spread_pct: Money
    bid_notional: Money
    ask_notional: Money
    #: (bid_notional - ask_notional) / (bid_notional + ask_notional), in [-1, 1].
    imbalance: Money


class InstrumentMeta(Stamped):
    """Exchange trading rules consumed by the risk engine (cached 24h, §2.1)."""

    symbol: str
    tick_size: Money
    qty_step: Money
    min_notional: Money
    contract_size: Money = Decimal("1")


class MarketHours(Frozen):
    """Crypto is always open; the v2 forex adapter returns real sessions."""

    always_open: bool = True
    venue: str = "binance_usdm"


# --------------------------------------------------------------------------- #
# News, sentiment, macro, FX
# --------------------------------------------------------------------------- #


class NewsItem(Frozen):
    """§2.2 — headline, source and freshness only. Body text is not fetched in v1."""

    title: str
    source_domain: str
    published_at: datetime
    age_minutes: int
    currencies: tuple[str, ...] = ()
    votes: int | None = None


class NewsContext(Stamped):
    provider: str  # "cryptopanic" | "rss"
    items: tuple[NewsItem, ...] = ()


class SentimentContext(Stamped):
    """alternative.me Fear & Greed: value, classification, yesterday delta (§2.3)."""

    value: int
    classification: str
    previous_value: int | None = None

    @property
    def delta(self) -> int | None:
        if self.previous_value is None:
            return None
        return self.value - self.previous_value


class MacroContext(Stamped):
    """CoinGecko global: BTC dominance and total market cap change (§2.3)."""

    btc_dominance_pct: Money
    total_mcap_change_24h_pct: Money


class FxRate(Stamped):
    """EURUSD for display/sizing conversion only (§2.4)."""

    pair: str = "EURUSD"
    rate: Money
    #: True when Frankfurter was unreachable and a cached rate was reused.
    is_last_known_good: bool = False


class GlobalContext(Frozen):
    """Cycle-wide context fetched once and shared by every symbol snapshot (§3)."""

    news: NewsContext | None = None
    sentiment: SentimentContext | None = None
    macro: MacroContext | None = None
    fx: FxRate | None = None


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


class MarketSnapshot(Frozen):
    """One symbol, one cycle. Reconstructable from the DB on its own (PRD F10)."""

    schema_version: int = 1
    snapshot_id: UUID = Field(default_factory=uuid4)
    cycle_id: UUID | None = None
    symbol: str
    captured_at: datetime
    last_price: Money

    ohlcv: dict[str, OHLCVSeries]
    instrument: InstrumentMeta | None = None
    derivatives: DerivContext | None = None
    orderbook: BookSnapshot | None = None
    news: NewsContext | None = None
    sentiment: SentimentContext | None = None
    macro: MacroContext | None = None
    fx: FxRate | None = None

    #: Populated by the feature engine in M2; ingestion never computes indicators.
    features: dict[str, Any] | None = None

    data_quality: DataQuality = DataQuality.OK
    degraded_fields: tuple[str, ...] = ()

    @property
    def is_degraded(self) -> bool:
        return self.data_quality is DataQuality.DEGRADED
