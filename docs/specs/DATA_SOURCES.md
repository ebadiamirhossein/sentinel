# SPEC — Data Sources & Accounts

**Module:** `sentinel/ingestion/`. All clients: async, per-call timeout, retry with backoff (max 2), response cached per cycle, every record stamped `{source, fetched_at}`.

---

## 1. Accounts / tools to prepare (shopping list)

| What | Needed? | Cost | Notes |
|---|---|---|---|
| Anthropic API key | ✅ Required | usage-based (main cost) | Console → API Keys; set a monthly spend limit anyway as a safety net |
| VPS (Hetzner CPX21 or similar, 2vCPU/4GB, EU) | ✅ Required | ~€8–12/mo | Ubuntu 24.04 + Docker |
| Telegram bot | ✅ Required | Free | @BotFather → token; get your numeric user id for the allowlist |
| Binance account (read-only API optional) | ⚪ Optional | Free | Public market endpoints need NO key; a keyless setup works for all v1 data |
| CryptoPanic API | ✅ Recommended | Free tier | News headlines with source & currencies tagging |
| TradingView | ❌ Not needed in v1 | — | We render our own charts; revisit only if you later want TV webhook alerts as an extra trigger |
| CoinGecko | ✅ | Free tier | BTC dominance, global market cap |
| alternative.me | ✅ | Free | Fear & Greed Index |

## 2. Per-source detail

### 2.1 Binance USDT-M Futures (primary market data — via `ccxt` + raw endpoints)
- **OHLCV:** 15m/1h/4h/1d, tail lengths **200/200/200/100 _closed_ candles** per symbol per cycle.
  - **The +1 rule (added 2026-08-18, from M2):** Binance returns the in-progress
    candle as the last row, and the feature engine drops it (indicators must not
    repaint mid-candle). So the ingestion layer requests **201/201/201/101** in
    order to yield the closed-candle tails above. Without the +1 only 199 closed
    candles remain and **EMA200 can never be computed** — which silently forced
    every trend regime onto a reduced EMA20/50 basis until it was caught. The
    numbers in `config.yaml` are therefore intentionally one higher than the
    tails specified here; this is not a typo.
  - **Chart timeframes carry more history (added 2026-08-18, from M3):** 15m/1h/4h
    request **321 (= 320 closed)**. An EMA200 is first defined at the 200th
    candle, so a 200-closed tail produces exactly **one** valid EMA200 point —
    nothing to draw a line between, while M3 requires EMA200 on the chart. 320
    closed yields 121 valid points, enough to span the 120-candle chart window.
    Still one API call per timeframe, and the extra rows dedupe away on upsert.
    `1d` keeps its 100-closed tail, so its EMA200 stays `null` by design.
- **Order book:** top-50 depth snapshot → computed bid/ask imbalance ratio (deterministic feature; raw book NOT sent to LLM).
- **Funding rate:** current + next; **Open interest:** current + 24h series; **Long/short account ratio:** top-trader ratio.
- **Exchange info:** tick size, qty step, min notional per symbol (cached 24h) — consumed by risk engine.
- Rate limits: comfortably within free public limits at 12 symbols / 60 min (15 min through M8.1); use ccxt built-in throttling.

### 2.2 News — CryptoPanic (+ RSS fallback)
- Filter: currencies matching watchlist + "important" flag; fields kept: title, source domain, published_at, currencies, votes. **Body text not fetched in v1** — the analyst judges relevance from headline + source + freshness (age_minutes).
- Fallback RSS (CoinDesk, CoinTelegraph, Bloomberg crypto tag) parsed with `feedparser` if CryptoPanic down.

### 2.3 Sentiment & macro
- Fear & Greed (alternative.me): value + classification + yesterday delta.
- CoinGecko global: BTC dominance, total mcap 24h change.
- (P1) Economic calendar for FOMC/CPI blackout windows — evaluate free sources (e.g., FRED release schedule or a calendar API) during M8.

### 2.4 EURUSD conversion
- Frankfurter.app (ECB rates, free, no key) fetched hourly, cached, with last-known-good fallback. Used ONLY for EUR display/sizing conversion.

## 3. Adapter interface (future-proofing for forex)

```python
class MarketDataAdapter(Protocol):
    async def ohlcv(self, symbol, timeframe, limit) -> DataFrame: ...
    async def derivatives_context(self, symbol) -> DerivContext | None: ...  # funding/OI; None for spot/forex
    async def orderbook_snapshot(self, symbol) -> BookSnapshot | None: ...
    async def instrument_meta(self, symbol) -> InstrumentMeta: ...  # tick/step/min-notional
    def market_hours(self) -> MarketHours: ...  # crypto: always open; forex adapter: sessions
```

`crypto_binance.py` is the only v1 implementation. A future `forex_oanda.py` implements the same protocol — no pipeline changes.

## 4. Data quality rules

- Each field carries `max_age`: OHLCV 2× timeframe; funding 15m; OI 30m; F&G 24h; news 6h window.
- Any core field (OHLCV) stale/missing → skip symbol. Any secondary field stale → snapshot `DEGRADED` with a list of degraded fields passed to the analyst.
- All timestamps UTC everywhere; owner timezone applied only at Telegram render time.
