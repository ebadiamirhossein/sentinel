# MILESTONES — Build Plan for Claude Code

Each milestone = one focused Claude Code session (or a few). **Rule: a milestone is DONE only when its tests pass and its demo command works.** Do not start the next milestone with failing tests. After each milestone, bring results back to the architect chat (Claude project) for review before continuing.

---

## M0 — Skaffold & foundations (½ day)
Repo layout per ARCHITECTURE.md; `pyproject.toml` (Python 3.12); Docker Compose (app + postgres) with healthchecks; `.env.example`; `config.yaml` defaults; structlog setup; Alembic init; CI-ish local scripts: `make test lint typecheck run`.
**Demo:** `docker compose up` → `/health` returns ok; `make test` green (placeholder tests).

## M1 — Ingestion + snapshot assembler (1–2 days)
`MarketDataAdapter` protocol + Binance implementation (ccxt); news, F&G, dominance, EURUSD clients; `MarketSnapshot` Pydantic model; staleness/degradation logic; snapshot persistence.
**Tests:** recorded-response (cassette) tests per client; staleness unit tests.
**Demo:** `python -m sentinel.tools.snapshot BTCUSDT` prints a full validated snapshot.

## M2 — Feature engine (1 day)
EMA/RSI/ATR/relative volume/regime classifier/S&R detector (pivot-based) as pure functions; golden-file fixtures (precomputed CSVs).
**Tests:** golden values match to 1e-8; regime classifier truth table.
**Demo:** snapshot tool now includes computed features.

## M3 — Chart renderer (½–1 day)
mplfinance dark-theme PNGs for 15m/1h/4h: candles, EMA20/50/200, volume, S/R lines, symbol+TF+UTC timestamp watermark. Deterministic given same OHLCV.
**Tests:** image generated, non-empty, dimensions correct; params persisted.
**Demo:** `python -m sentinel.tools.chart SOLUSDT 1h` writes PNG.

## M4 — Risk engine (1–2 days) ⚠️ TDD — write tests first from specs/RISK_ENGINE.md §8
Coherence checks, ladder builder, sizing/leverage/liq-buffer math, portfolio rails, pause state.
**Tests:** 100% branch coverage; property tests pass.
**Demo:** `python -m sentinel.tools.size --fixture examples/sol_long.json` prints a full TradePlan.

## M5 — LLM pipeline (1–2 days)
Anthropic client wrapper (retries, cost logging per call); screener batch call; analyst call with vision + structured output + schema validation + one-retry rule; prompt files v1; recent-history context block (stub stats until M7).
**Tests:** schema validation & retry-then-discard paths with mocked API; prompt assembly snapshot tests.
**Demo:** `python -m sentinel.tools.analyze SOLUSDT` runs real end-to-end analysis and prints the AnalystReport + gate result.

## M6 — Telegram bot (1–2 days)
Signal cards per specs/TELEGRAM_UX.md; buttons; commands `/capital /risk /status /positions /pause /resume /watchlist /settings`; user-id allowlist; idempotent posting.
**Tests:** card rendering snapshot tests; command handlers with fake bot.
**Demo:** real signal card appears in your Telegram with working buttons.

## M7 — Tracker, stats & orchestrator (2 days)
Cycle orchestrator wiring M1–M6 on APScheduler; tracker state machine with ladder-aware R accounting; stats module; `/stats`; daily-loss auto-pause; dedup/cooldown; crash-recovery from DB.
**Tests:** state-machine transition table tests; R accounting fixtures (incl. partial fills); restart-recovery test.
**Demo:** system runs 24h locally in "watch mode", produces signals, tracks a manually-marked one.

## M8 — Hardening & deploy (1 day)
VPS deploy runbook (`docs/DEPLOY.md`); pg_dump backup cron; failure alerts to Telegram; log rotation; spend guard (daily Anthropic cost log + alert threshold); README polish.
**Demo:** fresh VPS → running system in < 30 min following the runbook only.

## M9 — Shakedown (2 weeks, calendar time, no coding pressure)
Run live in signals-only mode. You mark Taken/Watch/Skip honestly. Weekly review in the architect chat: `/stats`, false-positive review, prompt v2 proposal.
**Exit criteria:** ≥ 25 tracked signals, JSON validity ≥ 98%, zero sizing bugs, and a first prompt-version comparison.

## M10 — Ensemble shadow mode (1 day)
OpenAI GPT-5.6 Sol as second AnalystProvider per docs/specs/ENSEMBLE.md §3; parallel shadow analysis; comparer; /stats compare-models.
**Demo:** signal card shows the 2nd-opinion footer; stats command splits by agreement.

## M11 — Consensus gate (½ day, feature-flagged, only if M10 exit criteria met)
Deterministic gating table per docs/specs/ENSEMBLE.md §4.

## Later (P1/P2 backlog)
Daily digest & weekly report → `/analyze` on demand → event blackout windows → backtest harness on stored snapshots → forex adapter → optional web dashboard → (much later, only if stats justify) approval-gated execution behind a hard feature flag.

**Total estimate: ~10–12 focused build days + 2 weeks shakedown.**
