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

## M8.6 — `/journal` and `/snapshot` (0.5 day)
Added 2026-08-20. Two gaps either side of what M8.1–M8.5 built: nobody could get their
own history *out* of the system, and the deterministic half of the pipeline — the half
with no AI in it — had never been readable from a phone.

* `/journal [30d|90d|all]` — the caller's own book as an **XLSX document**: one row per
  signal, one sheet per population (Real / Hypothetical / Undecided / Dry run) plus a
  Legend, never mixed. Running balance in net R and rolling win rate on gross R,
  computed within a sheet, so the Real sheet's last row equals what `/stats` reports.
* **Strictly caller-scoped, three ways**: a keyword-only `user_id` on the query, a
  `JournalRow` type with no field that could hold an id, and a filename carrying
  neither. `tests/bot/test_journal.py` proves A's file cannot contain B's rows at the
  view level, cell by cell through openpyxl, and by scanning the raw zip's XML — plus
  a deliberately leaking file to prove those checks can fail.
* **Open signals are rows with the running columns blank** (owner ruling): a record
  whose earlier balances renumber when an open trade resolves is not a record.
* `/snapshot SOLUSDT` — everything code computed for the last cycle and **no LLM
  content at all**: last price, per-timeframe regime with its `RegimeBasis`,
  EMA20/50/200, RSI, ATR and ATR%, relative volume, funding, OI and its 24h change,
  S/R with touch counts, orderbook imbalance, Fear & Greed, BTC dominance, data-quality
  flags. Labelled "computed by code, before any AI analysis"; every absent block named
  rather than dropped or zero-filled.
* Shared market data, so `/snapshot` is byte-identical for owner and member — the
  handler takes no `Actor` at all.
* **Read-only. No migration, no new table, no new column.** One new query
  (`SignalRepository.journal_since`) and one existing one (`latest_for_symbol`).
* One new dependency, **openpyxl** (owner-approved): 250KB wheel, pure Python, no
  native extension. XLSX numbers are IEEE doubles, so every figure is asserted to
  round-trip through the file unchanged.
* `/help` now splits across messages rather than losing prose — M8.5's ruling, applied
  to the one card that had run out of room.
* New spec sections `TELEGRAM_UX.md` §3c and §3d.
* **Post-deploy fix, same day.** `/snapshot` was silent for six of ten watchlist
  symbols: the feature engine's EMA-stack label (`20>50<200`) reached the card
  unescaped, Telegram read `<200` as an opening tag and refused the whole message.
  Fixed, plus a `tests/bot/telegram_html.py` validator that encodes Telegram's parsing
  rule (Python's own `html.parser` treats `<200` as text and would have passed the
  broken card), plus `answers_on_failure` on every member handler so a future failure
  replies instead of going quiet — M8.2 §1's lesson, met a second time.

## M9 — Shakedown (2 weeks, calendar time, no coding pressure)
Run live in signals-only mode. You mark Taken/Watch/Skip honestly. Weekly review in the architect chat: `/stats`, false-positive review, prompt v2 proposal.
**Exit criteria:** ≥ 25 tracked signals, JSON validity ≥ 98%, zero sizing bugs, and a first prompt-version comparison.

> **Read this before the rejection histogram (added 2026-08-22, from M11p; HANDOFF §4
> item 15).** **Sizing is not a scaling.** The same analysis sized at €200 and at
> €10,000 does not produce the same plan rendered twice — it produces two plans. On the
> golden BTCUSDT setup, three ladder rungs become **two**, the weights move 40/35/25 →
> 53.33/46.67, the stop distance moves -0.86% → -0.97%, and **TP1 net RR moves 1.75R →
> 1.50R**. `min_notional = max(exchange_min, 20 USDT)`, the quantity step and the
> leverage cap all bite in absolute terms, so a small account funds fewer rungs and pays
> a larger *share* of its risk budget in fixed costs.
>
> The owner's live capital is **€200**; every golden sizes at **€10,000**; and
> `min_rr_tp1 = 1.5` sits exactly where the €200 figure landed. So the measured sample
> comes from a region of the engine's behaviour no golden has ever pinned, and a
> `NET_RR_TOO_LOW` histogram read without this will credit the market for an artefact of
> account size.
>
> **Two questions to add to this review**, both answerable from stored `gate_decisions`
> and stored plans and neither from a golden:
> 1. what share of `NET_RR_TOO_LOW` rejections would have been approvals at a larger
>    capital?
> 2. how many delivered signals lost a ladder rung to `min_notional`?

> **Renumbering (2026-08-20, from M10a).** The owner asked for a second *market*
> before a second *model*, so M10 splits into **M10a** (the market dimension, below)
> and **M10b** (the forex adapter). Ensemble shadow mode moves M10 → **M11** and the
> consensus gate M11 → **M12**; the read-only dashboard becomes **M13**.
> docs/specs/ENSEMBLE.md carries a dated correction rather than being rewritten —
> this file is the forward plan, that one is the recorded decision.

## M10a — The market dimension (1–2 days) ✅
Added 2026-08-20. Teaches the system to hold more than one market **without adding a
second one**. No forex code whatsoever: no adapter, no provider, no spec.

* A `market` column on every table whose rows belong to one market (migration
  `0010`), backfilled to `crypto` and NOT NULL. `MarketScopedRepository` binds a
  repository instance to a market, so every write stamps it and every read filters
  on it — a decision made once per call site rather than on thirty methods.
* A `markets:` config block — `enabled`, `dry_run`, `adapter`, `watchlist`,
  `scan_interval_minutes` and an LLM budget, per market. A config file with **no**
  `markets:` block is still valid and reads as crypto-only, which is what the
  deployed server runs; a file carrying both shapes is refused at load.
* Per-market LLM sub-budgets under a **global ceiling** ($10 crypto + $4 forex
  against $11), so markets compete rather than stack. One market's overspend never
  stops another's analysis.
* Pause becomes four rails — global, per market, per user, per (user, market) —
  composed in `core/pauses.py` and handed to the **unchanged** risk engine as the
  single `PauseState` it has always taken. `/pause` with no argument is still
  system-wide.
* Statistics gain the market as a fourth dimension and it is the strict one:
  `StatsReport` has no field that could hold a merged figure, and `summarize()`
  raises on rows from two markets.
* One scan job per enabled market; with forex disabled, exactly one, at 60 minutes.

**Tests:** a golden-cycle regression suite built first — features, chart bytes,
assembled prompt, gate decision and rejection reason, and **every** Telegram surface
(`/status /stats /pulse /pulse 24h /pulse SYMBOL /positions /watchlist /snapshot
/journal`) asserted byte-identical, and asserted again when rendered from the frozen
pre-M10a config the server actually loads. A 16-row pause truth table. Explicit
proof that a forex overspend does not stop crypto, and the reverse.
**Demo:** `make check` green with every golden unchanged; `ops/verify-migration.sh`
takes a populated production dump up, down and up again with row counts intact.
**Exit criteria:** crypto output byte-identical, risk engine untouched (zero-line
diff, 100% branch coverage unchanged), migration verified against a populated copy.

## M10b-1 — Forex data spine and sizing core ✅
Build-order steps 1–6 of `docs/specs/FOREX.md` §14, shipped behind
`markets.forex.enabled: false` so deploying it is a **no-op** for the running crypto
system. What landed: a Saxo FxSpot `MarketDataAdapter` with the pip cross-checked
against `TickSize × 10` and paging forbidden outright; market hours, where *closed* is
a normal state with its own code and is checked before any staleness rule; the OAuth
chain hardened for a single-use rotating credential with a one-hour memory; a new
`sentinel/fx/` sizing module built test-first from hand-computed fixtures, with
`sentinel/risk/` untouched; measured-spread costs and a corrected net-RR model; and the
hand-maintained economic calendar with the staleness rail that stops an expired file
reading as "no events today". Migration `0011` makes `ohlcv_candles.volume` nullable —
forex has no volume of any kind, and §2.1 requires that to look missing rather than
zero — and adds `forex_instruments` and `saxo_oauth_tokens`.

Five further spec defects were found by checking FOREX.md against the code rather than
against the API, and all five are recorded dated in its corrections log; two of them
(#15's self-defeating spread baseline and #16's net-RR arithmetic) changed shipped
behaviour. **Answering M10a's two open questions:** symbols stay disjoint and the
`ohlcv_candles` primary key is **not** widened — the assumption is asserted at config
load instead, so an overlap is a file that will not load rather than candles that
silently interleave. Crypto's reserved budget floor lands with the wiring in M10b-2,
because it only bites once forex can spend.
**Exit criteria (met):** crypto goldens byte-identical, `git diff sentinel/risk/` empty,
risk branch coverage unchanged at 100%, `make check` green.

## M10b-2 — Forex features, charts and wiring (spec written — not implemented)
Steps 7–8: the added features (prior-day and prior-week levels, session labels, a
synthetic USD strength index, cross-pair correlation, the measured spread series), the
chart variant with **no volume panel at all** rather than an empty one, the forex
analyst prompt stating plainly which inputs are unavailable, and the orchestrator and
tracker wiring — still behind `enabled: false`. Split out of M10b because these need
visual checking, which is different work from the fixture-driven half and deserves its
own attention rather than the tail of a long session.

## M10c — Forex card, publishing and tracking ✅
Answers spec defect #12. `ForexPlan` is a **parallel** model in `sentinel/fx/`, not a
`TradePlan`, not a subclass and not a shared base class — it mirrors the shared
vocabulary field for field (which is what lets one signals table, one publisher and one
tracker serve both markets with no translation layer) and has **no field at all** for a
liquidation buffer, a funding rate or a derived leverage. The gate is a composition of
rails that already existed, in the order `risk/engine.py` established. `SignalRecord.plan`
widens to a union with **no migration**, and the hazard that creates — two look-alike
models a mis-dispatch could confuse — is answered by dispatching on `market` with no
fallback, and by a test asserting neither model's field set is a subset of the other's.
The tracker learns that a closed market is not a stall, and every forex ladder's expiry
is capped at the Friday close at **gate** time so none can rest over a weekend.

Three further spec defects, all dated in FOREX.md's corrections log. **#21**: crypto's
`max_entry_distance_pct: 3.0` could never fire in a market that moves 0.5% a day, and a
rail that cannot fire reads on a checklist as a rail — forex gets its own thresholds.
**#22**: `capital` and `risk_per_trade_pct` are the only two `TradePlan` fields the engine
does not quantize and both come from `Numeric(38, 18)` columns, so the live card printed
`€200.000000000000000000`; no golden could see it, because the fixture's capital is an
in-process `Decimal` that never round-trips through Postgres. **#23** is a defect in the
milestone *instruction*: §11's regeneration cannot execute in a milestone that also
requires `enabled: false`, so the tagged form is pinned as an **addition** instead and
switch-on day moves no golden at all.
**Exit criteria (met):** `markets.forex.enabled: false`, `git diff sentinel/risk/` empty,
risk branch coverage unchanged at 100%, `make check` exit 0, and the only crypto bytes
that moved are defect #22's two lines — approved in advance, line by line.

## M10d — Forex switch-on ✅
Added retrospectively 2026-08-29 (M12): this milestone and M10e shipped and are reported
in `journal/`, and this file stopped at M10c. Recorded here so the ledger and the journal
agree.

* `markets.forex.enabled: true`, 2026-08-21, opening a **two-week observation window**
  with `dry_run: false` — deliberately against FOREX.md §11. M7 settled that `dry_run`
  publishes *nothing*, and the point of the window is that the owner **reads** real forex
  cards and does not trade them. "Not trading" is his hand on the mouse, not a config flag.
* Spend ceiling raised 11 → 20 for the window, with a `llm_reserved_floor_usd: 8` held for
  crypto so an unmeasured market cannot compete with a measured one, and the **revert
  values written down beside the way forward** — a rail raised for a rehearsal and never
  lowered is how the $73/day incident started.
* **Review date now ~2026-09-07**, moved from 2026-09-04 by M12: five of the window's
  first days produced nothing (see M10e).

## M10e — The forex daily-staleness rail ✅
Added retrospectively 2026-08-29 (M12), same reason as M10d.

The first trading day a forex cycle ever ran on was Monday 2026-08-24, and it lost every
cycle of it. The 1d staleness rail compared **wall-clock** age against a 48-hour budget,
and on a Monday the newest *closed* daily bar is Friday's — 79 hours old at the first
cycle, 90 at the last. `status: OK`, `spend_usd: 0`, `/health` green, no alert.

* The 1d rail is now measured in **observed market hours**; intraday rails untouched; no
  threshold moved. New `forex.daily_freshness` log line every cycle.
* **`AlertKind.MARKET_BARREN`** — the rail for *nothing going right*. Every previous rail
  watched for something going wrong, and "all three pairs skipped, status OK, cost zero"
  was the blind spot rather than an edge case.
* **The lesson (HANDOFF §4 item 16):** a calendar-dependent rule tested on one day of the
  week has not been tested. Every forex test in the repo ran at a Wednesday anchor; the
  fixture was already correct. Forex tests now parametrise over both DST anchors, both
  ends of the scan window, and the Monday after a closed Friday.
* Spec ruling: FOREX.md §5.1, corrections-log defect #31. Its falsifying date was
  **Monday 2026-08-31**, which is after the measurement window closed.

> **M12 was briefly reassigned and the reassignment is reverted (2026-08-29).** The crypto
> correlation cap was scoped as M12 and **cancelled** before any rail was built — the rail
> would never have fired, because `check_portfolio_rails` reads the *taken* book and the
> evidence for it comes from the *published* one. Full reasoning and the surviving design:
> `journal/M9_STATS_REVIEW.md` (the banner and §4a) and `journal/M12_REPORT.md` §5.
>
> **So the numbering below is unchanged from the 2026-08-20 renumbering:** consensus gate
> keeps **M12**, dashboard keeps **M13**. The work that did ship under that name closed the
> M9 measurement window and is reported in `journal/`, which is where a cancelled milestone
> belongs — this file is the forward plan.
>
> Note that `journal/M11p_REPORT.md` is **M11p — Persian summaries**, a side milestone
> shipped out of band; it is not the M11 below.

## M11 — Ensemble shadow mode (1 day) (spec pending — do not implement)
OpenAI GPT-5.6 Sol as second AnalystProvider per docs/specs/ENSEMBLE.md §3; parallel
shadow analysis; comparer; `/stats compare-models`. Was M10 before M10a's renumbering.
**Demo:** signal card shows the 2nd-opinion footer; stats command splits by agreement.

## M12 — Consensus gate (½ day, feature-flagged, only if M11 exit criteria met) (spec pending — do not implement)
Deterministic gating table per docs/specs/ENSEMBLE.md §4. Was M11.

## M13 — Read-only web dashboard (spec pending — do not implement)
Read-only over the existing Postgres; auth for owner and members; hosted on the same
box behind the existing Caddy. Telegram stays the delivery surface and keeps the
buttons. Journal, pulse, stats and charts as pages. PRD P2 slot.

## Later (P1/P2 backlog)
Daily digest & weekly report → `/analyze` on demand → event blackout windows → backtest harness on stored snapshots → (much later, only if stats justify) approval-gated execution behind a hard feature flag.

**Total estimate: ~10–12 focused build days + 2 weeks shakedown.**
