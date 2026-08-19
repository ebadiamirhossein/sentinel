# ARCHITECTURE — Sentinel AI Trading Analyst

**Style:** Modular monolith (single Python app) + Postgres, packaged with Docker Compose.
**Why not microservices/n8n:** one operator, one box, tight data flow. A monolith with clean module boundaries is easier to test, debug, and evolve. Every module below is a Python package with a typed interface, so any of them can be split out later.

---

## 1. High-level flow

```
                    ┌──────────────── every 15 min (APScheduler) ────────────────┐
                    ▼                                                             │
┌──────────┐   ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────┴─────┐
│ Ingestion│──▶│ Feature │──▶│ Screener │──▶│  Deep    │──▶│  Risk    │──▶│  Telegram  │
│ (data)   │   │ Engine  │   │ (cheap   │   │ Analyst  │   │  Gate    │   │  Bot       │
│          │   │ (math)  │   │  LLM)    │   │ (Fable 5 │   │ (math,   │   │ (signals + │
│          │   │         │   │          │   │  +vision)│   │  no LLM) │   │  commands) │
└────┬─────┘   └────┬────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘   └─────┬──────┘
     │              │             │              │              │               │
     └──────────────┴─────────────┴──────┬───────┴──────────────┴───────────────┘
                                         ▼
                                  ┌──────────────┐         ┌──────────────┐
                                  │  Postgres    │◀───────▶│  Outcome     │ (runs every 1 min
                                  │  (audit +    │         │  Tracker     │  on taken/active
                                  │   state)     │         │  + Stats     │  signals)
                                  └──────────────┘         └──────────────┘
```

Principle (kept from the reference design, enforced harder here): **the LLM only does judgment — combining evidence into a thesis. Everything that must be exact, testable, and repeatable (indicators, sizing, leverage, limits, tracking) is deterministic code.**

## 2. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12, fully type-hinted | `mypy --strict` in CI |
| App framework | Plain async app + `APScheduler` for cycles; `FastAPI` only for `/health` | No web UI in v1 |
| Exchange data | `ccxt` (Binance USDT-M futures) + direct Binance endpoints for funding/OI/long-short ratio | Read-only; no API keys with trade permission ever |
| News | CryptoPanic API; RSS fallback (CoinDesk, CoinTelegraph) | Headlines + source + age only; analyst judges relevance |
| Sentiment | alternative.me Fear & Greed; CoinGecko BTC dominance & global mcap | Free |
| Indicators | `pandas` + `pandas-ta` | Unit-tested wrappers; no TA inside prompts |
| Charts | `mplfinance` → PNG (dark theme, EMAs, volume, S/R lines, last-candle timestamp printed on image) | Replaces TradingView screenshots; reproducible |
| LLM | Anthropic Messages API. Screener: `claude-sonnet-4-6`. Analyst: `claude-fable-5`, high effort, structured outputs (JSON schema), vision (chart PNGs) | Retry-once-then-discard on schema failure |
| DB | Postgres 16 + SQLAlchemy 2 + Alembic migrations | Single source of truth |
| Bot | `aiogram` 3 (async Telegram) | Inline keyboards for Taken/Skip/Watch |
| Config | `.env` (secrets) + `config.yaml` (watchlist, risk defaults, intervals) + runtime overrides stored in DB (set via Telegram) | Precedence: DB > yaml > defaults |
| Logging | `structlog` JSON logs; error budget → Telegram admin alert after 3 consecutive cycle failures | |
| Packaging | Docker Compose: `app`, `postgres`, (optional `grafana` later) | Healthchecks + `restart: unless-stopped` |
| Tests | `pytest`; golden-file tests for features & sizing; recorded-cassette tests for API clients | Risk engine: 100% branch coverage required |

## 3. Modules

```
sentinel/
├── ingestion/        # ccxt + news + sentiment clients; snapshot assembler
│   └── adapters/     # market adapters behind MarketDataAdapter protocol
│       └── crypto_binance.py   (forex_oanda.py = v2, same interface)
├── features/         # deterministic indicators, regimes, S/R detection
├── charts/           # mplfinance rendering → PNG bytes + stored params
├── llm/              # shared Anthropic client: retries, timeouts, cost logging,
│                     # untrusted-input containment (M5 — sits below both LLM
│                     # stages so neither has to import the other)
├── screener/         # cheap-LLM pass; strict JSON verdict per symbol
├── analyst/          # Fable 5 deep analysis; prompt assembly; schema validation
│   └── providers/    # AnalystProvider implementations (specs/ENSEMBLE.md §2)
├── risk/             # sizing, leverage, ladder builder, portfolio limits, pause rails
├── bot/              # aiogram handlers, signal cards, commands, callbacks
├── tracker/          # price-watch loop, fill/TP/SL/invalidation detection, R accounting
├── stats/            # win rate, avg R, profit factor, per-setup breakdown
├── storage/          # SQLAlchemy models, repositories, migrations
├── core/             # config, scheduler, cycle orchestrator, EUR conversion, clock
└── main.py
```

### Key dataflow contracts (Pydantic models)

1. **`MarketSnapshot`** — per symbol per cycle: OHLCV frames (15m/1h/4h/1D tail), computed features, funding, OI, L/S ratio, orderbook imbalance, F&G, BTC dominance, news items (headline, source, age_minutes), `data_quality: OK|DEGRADED`, timestamps.
2. **`ScreenerVerdict`** — `{symbol, interesting: bool, direction_hint, reason(≤200 chars)}`.
3. **`AnalystReport`** — thesis, setup_type (enum: trend_pullback, range_reversal, breakout_retest, momentum_continuation, mean_reversion), direction, entry_zone {low, high}, stop, targets[1..3], invalidation_text + invalidation_price, timeframe_label (intraday|swing), confidence 0–100, evidence[] (each item cites a snapshot field), candidate_status. **No sizes. No leverage. No EUR.**
4. **`TradePlan`** — everything above **plus** risk-gate output: entries[] {price, weight, qty, eur_notional, distance_pct}, margin_eur, suggested_leverage, risk_eur, rr_to_tp1/tp2 (gross **and** net of costs), the `costs` block, `last_price` with per-target `distance_pct`, and `gate_status: APPROVED_FOR_HUMAN | REJECTED(reason) | DOWNGRADED_WATCHLIST`. Every number the signal card shows is on it: the bot renders, it never computes.
5. **`SignalRecord`** — TradePlan + Telegram message ids + user decision + tracked outcome.

### Cycle orchestrator (core loop)

1. Assemble `MarketSnapshot` for each watchlist symbol (parallel, per-symbol timeout 20s; failures → symbol skipped this cycle, logged).
2. Screener batch call → keep `interesting == true` (typically 0–3 symbols).
3. For each candidate: render charts → analyst call (vision + JSON schema).
4. `candidate_status == CANDIDATE` → risk gate. WATCHLIST → store, optionally notify in daily digest. NO_SETUP → store verdict only.
5. Gate-approved plans → Telegram signal card. Everything persisted at every step.
6. Dedup guard: no new signal for a symbol while one is ACTIVE or within cooldown (default 4h) of a rejected/expired one, unless direction flips with strong evidence.

> **Correction (2026-08-19, from M8.1) — step 5 fans out; steps 1-4 never do.**
> The snapshot, the features, the screener verdict, the charts, the deep analyst call
> and the `analyst_reports` row happen **exactly once per symbol per cycle**, whoever
> is approved: the analyst produces a judgment about a market, not about a person,
> and it is the ~$0.32 tier. Only step 5 repeats — per eligible user, with that
> user's own `AccountState` and `PortfolioState`, their own `RiskEngine.evaluate`,
> their own `gate_decisions` row (the table gains `user_id`), their own signal and
> their own card. `sentinel/risk/` is untouched: it was already a pure function of
> `(account, portfolio)`.
>
> Step 6's guard runs on the **union** of eligible users — a symbol is analysed if
> *any* of them could receive it, because one member's cooldown must not suppress a
> shared analysis for everybody. It is applied a second time **per user** inside the
> fan-out, because the gate has no dedup rail of its own and PRD F11's "max 1 active
> signal per symbol" is per person. **Nobody eligible ⇒ the deep analyst is not
> called at all.**
>
> Contract 5 (`SignalRecord`) and the `signals` table gain `user_id`. One shared
> report becomes one row per user, each with its own sizing, decision, fills and
> realized R — so every per-user query downstream is a filter on a column rather than
> a join through a second table, and the tracker, the R accounting and the state
> machine need no changes at all.

> **Ruling (2026-08-18, from M7) — cooldown scope, and the flip exception deferred.**
> The cooldown also arms after a **stop-out**: re-entering the same failing idea on
> the next 15-minute cycle is exactly what the rail is for. "Unless direction flips
> with strong evidence" is **not implemented** — the spec never defines strong
> evidence, and an undefined threshold in a rail that governs money is a guess, not
> a feature. Deferred until the owner sets a number, ideally from M9's data.
>
> The guard runs **before** the deep analyst (an analyst call is ~$0.32), and the
> risk gate re-checks the same rails afterwards. The early check is an
> optimisation and never the only one. `core/orchestrator.select_symbols` is the
> pure function; it returns the reason each symbol was dropped, so a cycle that
> analysed nothing explains itself from its own row.

### Tracker loop (every 60s, only when active/taken signals exist)

- Pulls mark price; state machine per signal: `PENDING_ENTRY → PARTIALLY_FILLED → FILLED → (TP1_HIT → …) | STOPPED | INVALIDATED | EXPIRED`.
- Ladder-aware R accounting: realized R computed from actually-filled entries only.
- Posts threaded updates under the original Telegram message.
- Feeds `stats` tables; enforces daily-loss auto-pause.

> **Correction (2026-08-18, from M7) — TP hits are events, not statuses.** The
> `SignalStatus` enum shipped at M6 and `signals.status` has stored its values
> since; it has no `TP*_HIT` member and it does have `CLOSED`. Owner ruling: a
> signal stays **FILLED** while any size is open and becomes **CLOSED** when the
> last unit closes, and each target hit is a row in `signal_exits` and
> `signal_events`. Keeping the status a *state* rather than a partly-ordered
> history is what lets `tracker/machine.py` be one function of (status, event) —
> and lets the transition table cover every cell of `SignalStatus × EventKind`
> exhaustively, which docs/MILESTONES.md M7 asks for.
>
> **Correction (2026-08-18, from M7) — the price source is candles, not a poll.**
> "Pulls mark price" is what this section says; the tracker reads **1m high/low
> since the last tick** instead. A price sampled once a minute misses the wick
> that filled the rung or hit the stop, and that error is not symmetric — it
> under-reports stop-outs, so the measured win rate comes out better than the
> market was. Candles are also stored, so a detection dispute can be replayed.
> Invalidation is the exception and reads **closed 1h candles**, because
> specs/TELEGRAM_UX.md §4 words it as a close.
>
> **Ruling (2026-08-18, from M7) — a signal's first tick covers its whole life.**
> Found by the first live run: a signal published before the tracker existed had
> no `last_checked_at`, the window fell back to its floor, and the tracker judged
> the signal on its aftermath. The window now runs from `last_checked_at` **or**
> the signal's `created_at`.

## 4. LLM boundary rules (enforced in code)

- Analyst input = snapshot JSON + chart PNGs + account *limits* only (never balances-as-analysis-input beyond what's needed for context; sizing happens after).
- Analyst output schema-validated; numeric coherence re-checked deterministically: stop on correct side, targets ordered, entry zone sane vs. last price (≤ configurable % away), RR to TP1 ≥ 1.5 or reject.
- Prompts live in versioned files (`analyst/prompts/<provider>_vN.md`, per specs/ENSEMBLE.md §2); every stored report records the prompt version → enables A/B stats per prompt version. **This is the improvement loop the reference build lacks.**
- The **wire schema is part of the prompt surface**: structured outputs strips length and range bounds, so every capped field states its cap in its `description`. A test fails if the two drift (M5 — found by a live call that overran all three caps and cost a retry).

## 5. Deployment

- Any 2 vCPU / 4 GB VPS (Hetzner CPX21-class). Docker + Compose.
- `docker compose up -d`; volumes for Postgres; daily `pg_dump` to a mounted backup dir.
- Watchtower optional for image updates; healthcheck endpoint `/health` (scheduler heartbeat + DB ping + last-cycle age).
- Secrets only in `.env` (Anthropic key, Telegram token, CryptoPanic key). No exchange keys in v1.

> **Correction (2026-08-19, from M8) — the deployment target shares its host.**
> The box is a Hetzner CPX32 already running an unrelated application and its own
> Postgres, and several details of this section change as a result. The runbook
> is `docs/DEPLOY.md`; this is what it settles.
>
> * **Sentinel's Postgres publishes no host port at all.** 5432 belongs to the
>   neighbour, and the app reaches its database over the Compose network, where no
>   publish is needed. `docker-compose.dev.yml` adds a loopback publish for local
>   development only.
> * **The Compose project name is pinned** (`name: sentinel`), so containers and
>   the `sentinel_pgdata` volume can never collide with another project that
>   happens to share a directory name.
> * **`/health` binds `127.0.0.1:${SENTINEL_HTTP_PORT:-18080}`.** Telegram is long
>   polling, so the process needs no inbound port whatsoever; publishing one on a
>   shared public box would be exposure for nothing.
> * **`pg_dump` writes to a host directory through `docker compose exec`**, rather
>   than to a mounted backup dir as written above. Same outcome, and it avoids the
>   container-uid ownership trap on the mount. Retention, a Telegram alert on
>   failure, and a weekly *restore* into a scratch database are `ops/backup.sh` and
>   `ops/verify-backup.sh`.
> * **Watchtower is not used.** Automatic image updates on a system that trades no
>   money but reports numbers a human trades on is a way to change behaviour
>   without noticing. Updates are `ops/update.sh`, run deliberately.
> * **Log rotation is Docker's json-file driver**, capped per service in the
>   compose file (`10m` x `5`) rather than in `/etc/docker/daemon.json`, which the
>   neighbour application also depends on.

## 6. Failure modes & handling

| Failure | Behavior |
|---|---|
| Data source down | Symbol snapshot DEGRADED → analyst instructed to be stricter or return NO_SETUP; if core OHLCV missing → skip symbol |
| LLM invalid JSON | One retry with error feedback; then discard + log; never guess |
| LLM API outage | Cycle skipped, alert after 3 consecutive; tracker keeps running (no LLM needed) |
| Process crash | Docker restart; scheduler resumes; tracker rebuilds state from DB |
| Clock/staleness | Every snapshot stamped; analyst told data age; stale > threshold → degrade |
