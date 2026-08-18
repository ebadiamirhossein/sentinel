# M1 — Ingestion + Snapshot Assembler · Report

**Date:** 2026-08-18
**Status:** DONE — `make check` green (125 tests + 4 opt-in DB tests), live demo works, first schema migrated.

---

## 1. What was built

| Area | Delivered |
|---|---|
| Contracts | `ingestion/models.py`: `Candle`, `OHLCVSeries`, `DerivContext`, `BookSnapshot`, `InstrumentMeta`, `NewsItem`/`NewsContext`, `SentimentContext`, `MacroContext`, `FxRate`, `GlobalContext`, `MarketSnapshot`. Frozen Pydantic v2, `Decimal` money, every record stamped `{source, fetched_at}` (§3). |
| Adapter seam | `adapters/protocol.py` — `MarketDataAdapter` Protocol per §3 (`ohlcv`, `derivatives_context`, `orderbook_snapshot`, `instrument_meta`, `market_hours`). `forex_oanda.py` can drop in unchanged. |
| Binance | `adapters/crypto_binance.py` via ccxt `binanceusdm`: OHLCV 15m/1h/4h/1d, top-50 book → imbalance, funding + next settlement, OI current + 24h series, top-trader long/short, exchange rules (tick/step/min-notional) cached in-process. **Keyless.** |
| Clients | CryptoPanic + `feedparser` RSS fallback; alternative.me F&G; CoinGecko dominance/mcap; Frankfurter EURUSD with hourly cache and persisted last-known-good. All through one `HttpFetcher` (timeout, max-2 retries, backoff, structured errors). |
| Data quality | `staleness.py` — §4 as pure functions: core OHLCV stale/missing → skip symbol; secondary stale/missing → `DEGRADED` + named field list. Nothing is ever substituted. |
| Assembler | `assembler.py` — global context fetched **once per cycle** and shared; per-symbol `asyncio.wait_for` + exception boundary; `on_skip` callback; cycle summary log. |
| Persistence | `storage/models.py` + `repositories.py`: `market_snapshots` (JSONB context + sources + degraded fields), `ohlcv_candles` (upsert on `(symbol, timeframe, open_time)`), `instrument_meta`, `fx_rates`, `ingestion_failures`. Migration `0002_ingestion_schema`. |
| Tooling | `tools/snapshot.py` (the demo), `tools/record_cassettes.py` (refresh fixtures from live public APIs). |
| Tests | 71 new tests: adapter parsing from recorded ccxt output, client parsing + retry policy + RSS fallback, §4 truth table, per-symbol isolation, serialization, and 4 opt-in Postgres round-trip tests. An autouse fixture **fails any real socket connect**, so the suite provably never calls a live API. |

## 2. Demo

```bash
python -m sentinel.tools.snapshot BTCUSDT SOLUSDT       # print validated snapshots
python -m sentinel.tools.snapshot BTCUSDT --save        # + persist to Postgres
python -m sentinel.tools.snapshot BTCUSDT --json        # full model dump
```

Verified live on 2026-08-18:

```
── BTCUSDT ─────────────────────────────────────────
last_price    64255.1
data_quality  OK
ohlcv         15m:200, 1h:200, 4h:200, 1d:100
instrument    tick=0.1 step=0.001 min_notional=50.0
derivatives   funding=0.00006748 oi=105877.107 l/s=1.6392 oi_points=24
orderbook     imbalance=0.2274 spread=0.0002% levels=50
fear_greed    41 (Fear) delta=10
macro         btc_dominance=56.56% mcap_24h=0.78%
eurusd        1.1593
news          rss: 20 items
```

- `--save` twice → **2** `market_snapshots` audit rows, still **700** candles (upsert is idempotent).
- Pointing the F&G URL at an unreachable host → snapshot still produced, `data_quality=DEGRADED`, `degraded_fields=['fear_greed']`, failure logged with the reason. Explicit degradation, no invented value.
- `docker compose up` → app healthy, `alembic current` = `0002_ingestion_schema (head)`, `/health` → 200.
- DB round-trip: `SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://sentinel:change-me@localhost:5432/sentinel .venv/bin/pytest tests/ingestion/test_persistence.py` → 8 passed.

## 3. Decisions made

1. **Snapshot schema** (owner-approved): normalized `ohlcv_candles` + JSONB context on `market_snapshots`. A 15-minute cycle re-fetches ~700 candles per symbol; upserting keeps one row per candle instead of rewriting the series every cycle, and leaves M9's backtest harness a clean history to replay. Full provenance is kept in a `sources` JSONB column.
2. **Cassettes recorded live** (owner-approved): real Binance/alternative.me/CoinGecko/Frankfurter/CoinDesk payloads, recorded once by `tools/record_cassettes.py`. CryptoPanic is hand-authored from its documented shape because recording needs a key — noted in `tests/cassettes/README.md`.
3. **`ohlcv()` returns `OHLCVSeries`, not `DataFrame`** — see §4, this is the one deliberate deviation.
4. **Global context is fetched once per cycle**, not per symbol (§3 "response cached per cycle"). A 10-symbol watchlist makes one F&G call, not ten. Tested.
5. **Order-book imbalance** is notional-weighted over the top-50 levels: `(bid_notional − ask_notional) / (bid_notional + ask_notional)`, bounded −1…+1. §2.1 requires the ratio but does not define it; the raw book is never passed to the LLM.
6. **`httpx` promoted to a runtime dependency** (~0.5MB, under the 1MB approval bar). ccxt's bundled aiohttp was the alternative; httpx won because `MockTransport` makes cassette replay dependency-free. `pandas-stubs` added as a dev dep for `mypy --strict`.
7. **Missing CryptoPanic key → RSS, not an error.** §2.2 makes RSS the documented fallback, so an unkeyed setup is not by itself DEGRADED; RSS failing too is.
8. **Skipped symbols persist nothing.** `assemble()` raises `CoreDataMissing`, `assemble_many()` logs and drops it. The `ingestion_failures` table exists for the orchestrator to write cycle-level skip records in M7.
9. **Last-known-good FX is flagged, not silently reused.** `FxRate.is_last_known_good=True` marks the snapshot DEGRADED on `eurusd`, so the analyst is told the rate is cached.
10. **Test suite is provably offline.** The autouse `no_network` fixture blocks socket connects; only the opt-in Postgres tests carry `@pytest.mark.allow_socket`.

## 4. Deviations from spec

**One, deliberate and flagged for review:**

`MarketDataAdapter.ohlcv()` returns `OHLCVSeries` rather than the literal `-> DataFrame` in DATA_SOURCES §3. The same spec's preamble requires every record to be stamped `{source, fetched_at}`, and a bare DataFrame cannot carry that stamp or participate in the Pydantic contracts. The spec's DataFrame is one call away — `series.to_frame()` returns an indicator-ready float64 frame indexed by `open_time`, which is what M2/M3 will consume. **Say the word and I'll invert it** (return DataFrame, stamp separately), but the current shape keeps provenance and the contract intact.

**Endpoint note (not a deviation):** §2.4 names `frankfurter.app`; that host now 301-redirects to `api.frankfurter.dev/v1`. `config.yaml` points at the canonical host and says why.

## 5. Spec ambiguities resolved (assumptions, easy to change)

| Ambiguity | What I did |
|---|---|
| "Funding rate: current + **next**" | Binance's premiumIndex publishes the next settlement *time* but no predicted next rate; ccxt returns `None`. Stored as `next_funding_time` + optional `next_funding_rate`. |
| "OI 24h series" granularity | `period=1h, limit=24` (configurable). |
| Order-book imbalance formula | Notional-weighted, see decision 5. |
| News "6h window" | Treated as the freshness budget on the news fetch; items are not filtered by age, so the analyst can see that nothing recent happened. |
| Instrument `min_notional` | Taken from the exchange (BTCUSDT: 50 USDT, SOLUSDT: 5 USDT). RISK_ENGINE §4's 20 USDT floor is a *risk-engine* rule; M4 should take `max(exchange_min, floor)`. |

## 6. Notes for M2

- `OHLCVSeries.to_frame()` is the feature engine's entry point — float64, `open_time` index, columns `open/high/low/close/volume`.
- `MarketSnapshot.features` is already on the contract and stays `None` until M2 fills it.
- ATR(14, 1h) is what RISK_ENGINE §2 rules 3–4 need; the 1h series is always present in a non-skipped snapshot.
- M2 needs `pandas-ta` (>1MB → needs approval before it goes in `pyproject.toml`).
- `tests/cassettes/` gives M2 real candle series to golden-test against without any network.
