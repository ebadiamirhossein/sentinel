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

> **Added at M7 (2026-08-18, owner request), two guards that belong with the first
> unattended run rather than after it:**
>
> * **LLM spend guard — moved here from M8.** M8's "spend guard (daily Anthropic
>   cost log + alert threshold)" now ships in M7, because M7 is the milestone that
>   starts the scheduler: a bug, or a market event that makes every symbol look
>   interesting, can spend real money between midnight and breakfast. Daily and
>   monthly accumulators, `llm.daily_spend_limit_usd` (10) and
>   `daily_spend_warn_usd` (7), a Telegram notice when either trips, spend on
>   `/status`, and an auto-suspension of **new deep analysis only** — the screener
>   and the tracker keep running. M8 verifies it in the runbook instead of building
>   it.
> * **`dry_run` — first-cycle safety.** A top-level config flag that runs the whole
>   cycle (LLM calls, gate, persistence) and publishes nothing, logging the card it
>   would have sent. The signal is stored with `dry_run=true` and the tracker
>   resolves it silently, so 24 hours of it produces a *measured* paper record
>   rather than only an absence of crashes; `/stats` reports it as its own
>   population, never merged into real or hypothetical.
>
> **Demo, in the intended order:** 24h with `dry_run: true` and a silent phone,
> read `/stats`' DRY RUN population, then flip the flag and let the scheduler post
> the first real card.

> **Process change (2026-08-18, from M6.2 via M7) — packaging is in the gate.**
> `make check` now ends with `check-wheel` and `check-image`; the latter builds the
> Docker image **and imports the app inside it**. journal/M6_REPORT.md §12 found the
> image unbuildable for two milestones because tests, ruff and mypy all run from a
> source checkout. `make check-fast` is the old gate, for mid-edit runs.

## M8 — Hardening & deploy (1 day)
VPS deploy runbook (`docs/DEPLOY.md`); pg_dump backup cron; failure alerts to Telegram; log rotation; ~~spend guard (daily Anthropic cost log + alert threshold)~~ **→ shipped in M7, verify it in the runbook**; README polish.
**Demo:** fresh VPS → running system in < 30 min following the runbook only.

> **Scope settled at M8 (2026-08-19, owner) — the target box is shared.**
> Deployment is an existing Hetzner CPX32 (Ubuntu, Nuremberg) that already runs an
> unrelated application and its own Postgres. Three constraints follow, and they
> are asserted by `tests/test_compose.py` rather than only documented:
>
> * **Sentinel's Postgres publishes no host port.** The app reaches it over the
>   Compose network; `docker-compose.dev.yml` adds a loopback publish for local
>   development only.
> * **The app's host port is configurable** (`SENTINEL_HTTP_PORT`) and defaults to
>   **`127.0.0.1:18080`** — loopback, because Telegram is long polling and the
>   process needs no inbound port at all.
> * **The Compose project name is pinned to `sentinel`**, so containers and volumes
>   can never collide with the neighbour's.
>
> Two additions, both agreed before they were built:
>
> * **The spend guard's Telegram notice is finished here.** M7 shipped the guard
>   and both this file and journal/M7_REPORT.md §1 described "a Telegram notice
>   when either trips"; no code ever sent one. M8's job was to *verify* the guard,
>   verification found the hole, and it is closed through the alert channel this
>   milestone builds (journal/M8_REPORT.md §4).
> * **A host watchdog** (`ops/healthcheck.sh`, cron): nothing inside the app can
>   alert when the app is not running, which is the failure that matters most.

## M8.1 — Usability & multi-user (1 day)
Command menu via `setMyCommands`; plain-language `/help`; the `users` table with
owner approval (`/start` → request → `[Approve]/[Reject]`), `/users /approve /reject
/suspend`, a first-run acknowledgement recorded with its wording version, and
`/leave` so a member can remove themselves without asking. Per user: capital, risk %,
sized `TradePlan`, portfolio rails, decisions and statistics — from **one** shared
analysis per cycle.
**Demo:** two Telegram accounts receive the same setup, sized differently, and
neither `/stats` contains the other's decision.

> **The design constraint, and the two owner rulings (2026-08-19).**
>
> * **One analysis per cycle, shared.** The `AnalystReport` and the charts are
>   computed once; only sizing, rails, decisions and stats are per user. The analyst
>   is never run per person.
> * **`/pause` stays system-wide; the daily-loss pause becomes per user.** Members
>   have no `/pause`, so an owner without a system stop button would be worse than
>   the asymmetry.
> * **`/users` shows standing, join date, capital-set (yes/no) and loss-paused —
>   and nothing else.** No amount, no P&L, no decisions. Operating a system for
>   friends does not require watching them trade.

## M8.2 — Spend control, and the dead admin surface (1 day)
Added 2026-08-19 after the first live day measured $0.76/cycle against M7's ~$2.2/**day**
estimate — 2.63 analyst calls per cycle where ~24/day was budgeted.

* **The bug found on deploy:** every owner command (`/status /settings /watchlist /pause
  /resume /users /approve /reject /suspend`) and both admin buttons were silent, because
  `AuthMiddleware` was an *inner* middleware while `OwnerOnly` is a *root filter* —
  aiogram resolves root filters first, so `actor` was never injected. Silence is what
  `OwnerOnly` produces for a non-owner, so a dead router looked exactly like working
  access control. Fixed with `outer_middleware`; `tests/bot/test_dispatcher_wiring.py`
  drives a real `Dispatcher` and asserts **positive reachability** for every owner
  command, with a meta-test against `menu.OWNER_COMMANDS`.
* `scan_interval_minutes` 15 → **60** (the setup timeframe is 1h);
  `stale_cycle_multiplier` 2 → 1 so detection stays near an hour.
* `SkipReason.RECENTLY_ANALYSED` — a symbol whose last verdict was `WATCHLIST`/`NO_SETUP`
  is not re-analysed for one setup-timeframe candle. Shared, not per user.
* `cycles.skipped` JSONB (migration `0008`) with a closed `SkipReason` vocabulary, so M9
  can answer "what is the system declining to analyse, and why" in SQL rather than from
  logs that rotate.
* `screener_v2.md`, selected by `llm.screener_prompt_version`; `screener_v1.md` kept.
* Over-long `thesis`/`counter_thesis` are **truncated and logged**, not discarded — one
  live call was rejected at one character over, at ~$0.27.
* `margin_budget_pct` is a **hard** limit (`MARGIN_BUDGET_EXCEEDED`). Reduces signal
  frequency wherever `stop_fraction < risk_per_trade_pct`.
* `analyst_reports.llm_call_id` is finally populated.

Deferred to after re-measurement: prompt caching, and `analyst_effort`.

## M8.3 — Member watchlist requests (0.5 day)
Added 2026-08-19. Members can ask for a symbol without being able to spend on one.

* `/request SOLUSDT` for members, validated against the exchange at the door; the
  owner gets an approve/decline card naming who asked and where the watchlist stands.
* **One pending request per symbol**, enforced by a partial unique index
  (`uq_watchlist_requests_one_pending`), so a duplicate is a no-op that reaches nobody.
  Decided rows are kept, and a declined symbol may be asked for again — markets change.
* `watchlist_max_symbols` (config, default 15) **binds the owner's `/watchlist add`
  too**, and is re-checked at approval time. A request that cannot be granted because
  the list filled up stays PENDING and both people are told, rather than being declined.
* Migration `0009_watchlist_requests`.
* `/request` on the member menu; absent from the owner's, like `/leave`.
* The dispatcher-wiring meta-test now covers `MEMBER_COMMANDS` as well as
  `OWNER_COMMANDS`, discharging the member half of journal/M8_2_REPORT.md §1a's audit
  item 2 as a side effect of building on that surface.

## M8.4 — `/pulse`, the pipeline transparency command (0.5 day)
Added 2026-08-20. Closes journal/M8_1_REPORT.md §13 item 4: nobody but the owner could
see what the pipeline was doing, and M8.2's spend controls made quiet the expected state.

* `/pulse` — the last **completed** cycle's story: what the screener escalated with its
  reason, what was never analysed and why (`SkipReason`), each analyst verdict, and the
  gate's outcome. `/pulse 24h` is the same four sections counted over the day.
* **Available to every approved user** — the only such command. The analysis is bought
  once and shared, so its reasoning is identical for everybody and is shown that way.
* **Read-only. No migration, no new table, no write.** Screener verdicts come out of the
  `llm_calls` audit row (they have never had a table), skips out of M8.2's
  `cycles.skipped`, verdicts out of `analyst_reports`, outcomes out of `gate_decisions`.
* Two closed classifications carry the privacy boundary, each with a meta-test:
  `RejectionReason` splits shared/personal exactly where the gate starts reading an
  account (`PAUSED` and `NET_RR_TOO_LOW` are personal, and both look shared), and every
  `SkipReason` is rendered in words that name nobody, with the stored `detail` dropped.
* The spend line is the owner's alone, carried as `PulseView.spend is None` for a member
  rather than as a role check in the renderer — M8.1 §6's mechanism, one surface over.
* `/pulse` on both menus; `tests/bot/test_dispatcher_wiring.py` proves reachability
  **for both roles** through a real `Dispatcher`, and silence for a caller with no
  standing. Postgres round-trips for all three new reads, per M8.3 §4.
* New spec section `TELEGRAM_UX.md` §3b, superseding §3's unbuilt "market regime
  summary (P1)" row under the same name.

## M8.5 — `/pulse <SYMBOL>`, the drill-down (0.25 day)
Added 2026-08-20, hours after M8.4. `/pulse` and `/pulse 24h` are summaries — every
piece of prose on them is cut to fit a phone — so the question a reader has after
reading one ("why did it say *that* about ETHUSDT?") was the question they could not ask.

* `/pulse SOLUSDT` — one symbol's last analyst verdict with **nothing truncated**: the
  full thesis, the evidence list with each claim's source field, the counter-thesis,
  confidence, setup type, invalidation text, the data-quality note, provenance, and the
  gate's outcome. Three of those live only in `analyst_reports.report` as JSONB and had
  never been read by anything.
* **Splits across messages rather than shortening.** Not "two": `Evidence.claim` has no
  length bound at all, so the page count is computed, not assumed.
* **No prices.** `entry_zone`, `stop`, `targets` and `invalidation_price` are on the
  stored report and absent from the card — levels for a gate-rejected symbol would be an
  unsized trade suggestion with no approval behind it. The prose invalidation stays.
* The gate half reuses M8.4's `gate_outcome`, so the SHARED/PERSONAL boundary has one
  implementation and the parametrized sweep covers both surfaces through it.
* Read-only, no migration. One new query, `AnalystReportRepository.latest_for_symbol`.
* Same access as `/pulse` (all approved users), reachability proven for both roles.

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
