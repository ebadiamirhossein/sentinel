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
    differently. Fixed by adding SOLUSDT (1..1000) and DOGEUSDT (<1) as second
    and third golden symbols — an *addition*: the 23 existing fixtures were not
    regenerated. Proved by perturbing each branch in turn and watching exactly
    one golden fail each time, a different one each time. A structural test
    asserts the three symbols still *partition* the branches, so the set cannot
    collapse into covering one branch three times. **When adding a golden, ask
    which branches of which functions the chosen case actually reaches —
    coverage is of inputs, not of code, and one input reaches one branch.**
    Note also that `record_cassettes` re-recorded every symbol on every run, so
    adding one would have refreshed the others and moved every golden: it now
    takes `--symbols`.
11. **A milestone boundary is untested by construction.** M10b-1 shipped a Saxo
    adapter whose output could not be drawn (`volume=True` against an all-NaN
    column) and all 1777 tests passed: both halves were tested and the join
    between them belonged to the next session. When a boundary splits a producer
    from its consumer, the next milestone **composes them first and builds
    second**. M10b-2 §12 names the four joins its own boundary with M10c leaves
    untested for the same reason.

12. **A golden built from in-memory values cannot see a defect the database
    round-trip introduces.** `tests/golden/pipeline.py` sizes against
    `Decimal("10000")`, constructed in Python. The live system reads
    `users.capital_eur` from a `Numeric(38, 18)` column and gets
    `Decimal('200.000000000000000000')`. The two differ in **scale**, not in value,
    so the card printed `capital €200.000000000000000000` for 54 cycles while the
    golden stayed green — and no amount of *more* golden coverage would ever have
    found it, because the blind spot is structural: the fixture cannot produce the
    input that breaks. Found in M10c only because forex needed the same line.
    **Which other goldens have this blind spot:** every value on the cycle golden
    except two is quantized by `sentinel/risk/` before it reaches the card, so scale
    is pinned upstream and the fixture's provenance does not matter.
    `capital_eur` and `risk_per_trade_pct` were the exceptions — the only two
    `TradePlan` fields assigned without `money()`/`percent()` — and both are fixed.
    The **surfaces** goldens are the remaining exposure: `StatusView`,
    `SettingsView` and `UserView` carry raw `Decimal`s straight from repository
    rows, and `tests/golden/surfaces.py` builds those rows in memory too. Nothing
    there is currently wrong, and nothing there would show it if it became wrong.
    **When a surface renders a value that came from a `Numeric` column, assert on
    its scale, not only on its value** — or render it through
    `bot/formatting.money_eur`, which makes the scale the renderer's business
    instead of the column's.

13. **A test that looks like it is checking and is not is worse than no test.**
    `test_no_arithmetic`'s layer 2 compared every rendered number to the plan's
    numbers **as strings** — and `str(Decimal("200.000000000000000000"))` *is* what
    the card printed, so the check passed with the defect in front of it. It
    consumed the attention that would have found the gap: the file reads like a
    thorough guard, it has a proof-of-teeth test, and it was blind to an entire
    class of defect. It now compares by **value**, with an explicit third layer
    stating the rule it was missing — *the renderer may fix a display scale; it may
    never change a value.*
    **The pattern worth copying is the proof of teeth, and specifically how it is
    built:** when a check is loosened, the new test must break on the smallest thing
    the check still has to catch. `test_the_scale_check_would_catch_a_changed_value`
    perturbs €4570.30 to €4570.**31** — one digit, the same scale — so it fails
    unless the value comparison genuinely works. A proof that mutated the number
    beyond recognition would have passed against a check that had stopped working.


14. **A dev extra must never constrain a production dependency's version.** It is
    the same root cause as the `httpx` break, arriving from the opposite direction.
    There, a dependency production needed was declared by nobody and arrived
    transitively. Here, `pandas-ta` — a **test oracle**, in `[project.optional-
    dependencies].dev` — pulls `numba`, which refuses NumPy above 2.2. So `.venv`
    resolved **numpy 2.2.6** and a freshly built image resolved **2.5.2**, and the
    suite had never once validated the numpy the container runs. numpy sits under
    pinned matplotlib and mplfinance and under every RSI, ATR and EMA value, so the
    thing being silently version-controlled by a test-only package was the numerical
    core of the product.
    **What makes this class invisible is that the constraint is invisible where it
    matters.** Every check — tests, mypy, `check-deps`, even `check-wheel` — runs in
    the environment the dev extra shapes. The image is the only place the constraint
    is absent, and the image is the one place nothing was comparing versions.
    Settled in M10c by pinning `numpy==2.2.6` so `.venv`, the image and the goldens
    name one number. **`pandas` is still `>=2.2` and sits in exactly the same
    position** — its own decision, deliberately not taken in M10c.

    > **Correction (2026-08-21, from the hygiene session) — pandas is now decided,
    > and the measurement contradicted the assumption in the sentence above.**
    > `pandas==3.0.5`, and `ccxt==4.5.75` with it. The text above is left as written
    > because it is the record of what M10c chose to defer.
    >
    > It was **not** a repeat of the numpy story. `.venv` and the image *already
    > agreed*, at 3.0.5, which is why `check_pins` never warned: a divergence is the
    > only thing it can see, and two environments floating in step look exactly like
    > two environments pinned. So the pin moved no version and could not move a
    > golden — verified, 37/37 byte-identical — and what it bought was turning an
    > accident into a decision.
    >
    > The assumption worth correcting is "still `>=2.2`". That is the *declaration*;
    > the *resolution* on both sides had already crossed into **pandas 3.x** by
    > ordinary upgrade, with nothing recording it. A floor of `>=2.2` under a stack
    > running 3.0.5 is the same defect one level up from the one this item describes:
    > the declaration stopped describing the software some time ago and nothing said
    > so. That is the argument for pinning rather than for widening the floor.
    >
    > `uvicorn` is deliberately left unpinned and warning — it serves a loopback
    > `/health` endpoint and touches no number the product produces, and one live
    > divergence is what keeps `check_pins`' WARN branch observable in a real run
    > instead of exercised only by its own unit tests. See
    > journal/HYGIENE_2026-08-21.md §2.

    **`check-deps` cannot detect this class, and it should not be extended to.** It
    reads the *source tree* and asserts every third-party import is a declared
    dependency — a question about **declaration**, answered without resolving
    anything. This is a question about **resolution**: whether two environments
    resolve the same version of a shared package, which cannot be answered without
    building both. Bolting it on would make a fast, hermetic check depend on a Docker
    build, which is the thing `check-fast` exists to avoid.
    **It belongs in `check-image`, which already builds the image and already imports
    the app inside it — and it is now there** (`sentinel/tools/check_pins.py`, M10c).
    For every *pinned* dependency it asserts the image's installed version equals
    `.venv`'s and fails naming both; unpinned divergences **warn**, because a `>=`
    declaration is an explicit statement that the version may move and failing on it
    would make the gate lie about its own intent — but that warning is the form in
    which this would have caught numpy *before* anybody pinned it.
    One assertion covers this defect and M10b-1 §3's three-versions-of-`anthropic`
    defect, at build time in a gate rather than at boot on the server. Proved by
    skewing `matplotlib`'s pin, rebuilding the image and watching it exit 1 naming
    both versions.
    **On its first real run it found two more:** `ccxt` (the exchange client) and
    `uvicorn` already differ between `.venv` and a fresh image. Both are unpinned, so
    both warn — which is the check working, and `ccxt` is worth a decision.


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
