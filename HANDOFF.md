# HANDOFF — Sentinel Architect Chat v2

**Date:** 2026-08-20. This document transfers full context from the first architect
chat (spec → M0..M8.4 → live deployment) to a new one. Read it together with the
spec pack in project knowledge (PRD, ARCHITECTURE, MILESTONES, RISK_ENGINE,
PROMPTS, TELEGRAM_UX, DATA_SOURCES, ENSEMBLE) and the journal reports in the
repo (journal/M*_REPORT.md), which are the ground truth for what was actually built.

---

## 1. Working model (unchanged)

- **This chat = architect/reviewer.** Claude Code = builder. The owner (Amirhossein)
  pastes prompts into Claude Code and returns milestone reports here for review.
- The architect writes EVERY Claude Code prompt. The owner does not write prompts
  himself and prefers explicit step-by-step instructions (which button, which
  command, new chat or same chat).
- One Claude Code session per milestone. Plan mode + Opus for hard/risky milestones,
  Sonnet for routine ones. Never "skip all permissions".
- Milestone gate: `make check` green (tests, ruff, mypy --strict, wheel+image
  import, ops checks) + journal/M{N}_REPORT.md + push + (usually) deploy.

## 2. What exists and runs today

**System:** "Sentinel" — 24/7 crypto research/signal system. Analyzes; a human
trades. NO execution code, NO trade-capable API keys, ever (hard boundary).

**Live deployment:** Hetzner CPX32 (root@78.46.240.136, Nuremberg), /opt/sentinel,
Docker Compose project `sentinel`, container-internal Postgres (no published port),
health on 127.0.0.1:18080 (loopback only; Telegram long-polls so zero inbound
ports). Neighbour apps on the same box are systemd-native (fonderis-worker,
english-bot, Caddy, native Postgres on 5432) — do not touch.
ops/: update.sh (deploy), backup.sh + verify-backup.sh (daily 03:10/03:40 UTC
cron, restore-tested), healthcheck watchdog (*/5), log rotation. Reboot-tested.

**Pipeline (hourly cycle):** ingest (ccxt/Binance public, CryptoPanic+RSS, F&G,
BTC dominance, EURUSD via Frankfurter) → deterministic features (hand-rolled
Wilder RSI/ATR — pandas-ta is only a test oracle; EMA stack + slope regime with
TRANSITIONING label; pivot S/R with ATR clustering; closed candles only, tails
201/201/201/101... 1d=251 after raise) → chart PNGs 1600×1000 (dark, EMAs,
volume, S/R, watermark; byte-deterministic; vision input) → screener
(claude-sonnet-4-6, one batched call, screener_v2 prompt) → deep analyst
(claude-fable-5, vision + structured outputs, prompt fable_v1; evidence[],
counter_thesis, liquidity-trap check, data_quality_note; injection defense via
<untrusted_news_data> + defang, live-tested) → deterministic risk gate →
Telegram card → tracker (60s, 1m-kline fill/TP/SL detection, stop-first on
ambiguous candles, ladder-aware partial-fill R, realized costs) → stats.

**Risk engine (sentinel/risk/, 100% branch coverage, Decimal, no LLM):**
risk_per_trade default 0.75%; entry ladders 40/35/25 risk-weighted (NOT
notional-weighted — spec was corrected); coherence checks incl. 0.6–3×ATR stop
bounds, ≤3% entry distance, confidence ≥60; FX EUR→USDT is MULTIPLY by
Frankfurter rate (spec had it inverted); min_notional = max(exchange_min, 20
USDT) (BTC=50!); leverage derived, cap 10x, liq buffer ≥2× stop distance;
margin_budget_pct 10% is a HARD cap (reduces leverage, rejects if impossible);
fees (maker in 0.02%/taker out 0.05%) + funding estimated; **min_rr_tp1=1.5
gates NET RR** — this tightened the gate materially (tight-stop setups lose
~0.07%/stop_dist to costs). RejectionReason enum (~24 codes) persisted per
decision; SHARED vs PERSONAL classification exists for display boundaries
(PAUSED and NET_RR_TOO_LOW are PERSONAL — reasoning in M8.4 report).

**Cost controls (M8.2 — after a $73/day incident):** 60-min cycles; funded-user
check BEFORE analyst spend; re-analysis cooldown (one setup-TF candle after
WATCHLIST/NO_SETUP, SkipReason RECENTLY_ANALYSED); screener_v2 (stricter, but
measured only as volume control — WATCHLIST share of escalations did NOT drop:
100% on n=4 vs 55% under v1; quality unproven); daily LLM spend guard $10
(pauses NEW analysis only; tracker structurally cannot import LLM clients);
string caps live in field descriptions (structured outputs strips maxLength!),
overruns truncate-and-log. Current run rate ≈ $2–7/day.

**Multi-user (M8.1/M8.3):** users table, owner approval flow (/users /approve
/reject /suspend, partial unique index = single OWNER), per-user capital/risk/
rails/decisions/stats from ONE shared analysis per cycle (never per-user analyst
calls); first-run risk acknowledgement; /leave; /request SYMBOL → owner
approve/decline (watchlist cap 15, live-count enforcement at approve time).
Owner sees member status/join/capital-set(y/n)/loss-paused ONLY — never
amounts, P&L, or decisions (boundary is typed + tested).
Current users: owner Amirhossein (7222549221) + one member friend (1958877587).

**Telegram surfaces:** signal cards (2dp percentages, gross+net R, cost block,
ladder with signed distances, expiry; buttons Taken/Watching/Skip + Closed
manually/Note); /help (plain-language); command menus per role via
setMyCommands; /status /positions /stats [window] /capital /risk /watchlist
/pause /resume /settings; /pulse (last cycle: escalations+reasons, skips in
neutral words, verdicts, gate outcomes; owner-only spend line — boundary is a
type), /pulse 24h, /pulse SYMBOL (full untruncated thesis/evidence/counter/
data_quality_note, two-message split, cap-layer annotations, NO raw
entry/stop/target prices — rejected setups must not read as trade suggestions);
/snapshot SYMBOL (pure pre-AI math view; html.escape lesson — see §4);
/journal (XLSX export, caller-scoped, Real/Hypothetical sheets never mixed,
append-only running R, active sheet = first non-empty).

**Stats:** three populations — REAL (taken), HYPOTHETICAL (watched/skipped),
DRY_RUN — never merged. Per setup_type and prompt_version breakdowns exist.
**Current state: REAL=0, HYPOTHETICAL=2** (signal #2 ADAUSDT long
breakout_retest — filled 0.1838, TP1 hit +0.78R gross, stop at BE, open;
signal #4 ETHUSDT trend_pullback — pending entry). Owner capital €200
(deliberately small "rehearsal" size), risk 0.75%, EUR display via Frankfurter.

## 3. Where the plan stands

- **M9 (analysis, not code): first stats review ~Sept 3.** /stats all, /pulse
  24h patterns, gate rejection histogram, screener v1-vs-v2 comparison, decide
  prompt fable_v2 based on evidence. Exit criteria ideas in MILESTONES.md.
  ALSO on M9 list: html-escape audit of remaining renderers; journal
  active-sheet fix shipped already; risk_state.updated_at fix (done).
- **Owner's new asks (this handoff's reason):**
  1. **Forex version** — must NOT disturb crypto. Design: new MarketDataAdapter
     (interface exists in ingestion/adapters), pairs EURUSD/GBPUSD/USDJPY,
     market-hours aware, separate watchlist + per-market spend sub-budget +
     per-market stats populations. Honest gap: no funding/OI/true volume —
     compensate with economic-calendar blackouts (FOMC/CPI/NFP), DXY as regime
     anchor, stronger news layer. First decision: data provider (OANDA vs
     Polygon vs Twelve Data — evaluate cost/quality/ToS).
  2. **Multi-model ensemble** — extend ENSEMBLE.md: providers for GPT-5.6 Sol,
     Gemini 3.1 Pro, + a cheap Anthropic voice, ALL in shadow mode first
     (signals stay Fable-driven), comparer + /stats compare-models, consensus
     gate only for providers that earn it on ≥40 candidates (veto-only, never
     create; fail-open to primary). Cost multiplies per shadow provider — keep
     per-provider spend lines.
  3. **Web dashboard** — read-only over existing Postgres, auth for owner+
     members, host on same box behind existing Caddy; Telegram remains delivery
     +buttons. Journal/pulse/stats/charts as pages. PRD P2 slot.
- Suggested numbering: M10 forex adapter, M11 ensemble shadow, M12 consensus
  (flag-gated), M13 dashboard — order negotiable; the architect should sequence
  deploys so the crypto sample stays clean (batch restarts).

## 4. Hard-won lessons (do not relearn these)

1. **Silence-as-success is indistinguishable from failure.** Every filter/
   handler/alert path needs a POSITIVE reachability test on real machinery
   (dead admin router 4h; /snapshot silent 400). Meta-tests enforce this.
2. **Telegram + parse_mode=HTML: escape at the value seam** (`<0.207` killed a
   message; price-dependent bug). Audit remaining renderers in M9.
3. **Structured outputs strips maxLength** — caps go in descriptions; truncate-
   and-log, don't discard (a call died over ONE character).
4. **Test doubles hide DB/API reality:** dirty-DB suite wiped prod-adjacent
   rows (db_guard now refuses same-DB runs); text() upsert bug only on real
   Postgres; render-against-production is standard practice before shipping
   user-facing surfaces (caught 4+ bugs).
5. **Docker image must be built+imported in CI** (`make check-image`) — it was
   unbuildable for two milestones with all tests green.
6. **One bot token = one poller** — local `make run` fights the server.
7. **Spec defects are found by builders:** FX inversion, min_notional, ladder
   weighting, 100<200 candle contradiction — welcome pushback, record dated
   corrections in the spec files.
8. **Charts:** vision legibility drove 1600×1000/120 candles; EMA200 needs 200
   CLOSED candles (+1 rule); determinism = pinned matplotlib/mplfinance.
9. **Owner communication:** simple English, no jargon, explicit next actions;
   he screenshots Claude Code questions here — answer with exact option numbers
   and text to paste. Farsi occasionally — answer in kind.
10. **A golden pins the case it was built from, and nothing else.** The cycle
    golden covered BTCUSDT alone — one symbol, one price magnitude, above 1000.
    `charts/renderer._format_price` branches at 1000 and at 1, so a change to the
    1-to-1000 branch would have silently moved the stored chart bytes of LINK,
    AVAX and LTC — three live watchlist symbols — with the whole suite green.
    Found in M10b-2 only because forex needed that formatter to behave
    differently. Fixed by adding SOLUSDT as a second golden symbol (an
    *addition*; the 23 existing fixtures were not regenerated), and proved by
    perturbing the branch and watching only the new golden fail. **When adding a
    golden, ask which branches of which functions the chosen case actually
    reaches — coverage is of inputs, not of code.**
11. **A milestone boundary is untested by construction.** M10b-1 shipped a Saxo
    adapter whose output could not be drawn (`volume=True` against an all-NaN
    column) and all 1777 tests passed: both halves were tested and the join
    between them belonged to the next session. When a boundary splits a producer
    from its consumer, the next milestone **composes them first and builds
    second**. M10b-2 §12 names the four joins its own boundary with M10c leaves
    untested for the same reason.

## 5. Boundaries that must survive any new feature

- No execution code / trade-capable keys. Ever. (v1 hard boundary; revisit only
  after M9+ evidence, behind a default-off flag, explicit owner decision.)
- LLM never sizes/levers/approves; deterministic modules never import LLM.
- Members never see: owner's spend, other users' capital/decisions/P&L, raw
  prices for non-approved setups. Boundaries are types + tests, not habits.
- Populations never merge (REAL/HYPOTHETICAL/DRY_RUN; per-market next).
- Prompts change only by new version file + PROMPT_LOG entry; stats compare by
  prompt_version.
- Sentinel's Postgres never publishes a host port; app binds loopback only.
