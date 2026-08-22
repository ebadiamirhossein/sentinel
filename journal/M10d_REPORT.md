# M10d — forex switch-on

## State at handoff

> **This section is a snapshot taken at handoff and is deliberately not updated.**

> **Five defects in code the specs called done — that is the milestone. Forex is the
> side effect.** (Owner, 2026-08-21, on reading the handoff.)

**Branch `m10d-forex-switch-on`, 8 commits off `main` at `90f905f`. `main` is untouched.
Nothing is pushed and NOTHING IS DEPLOYED.**

`make check` **exit 0**. 2132 passed, 68 skipped. `mypy --strict` over 310 files. Risk
branch coverage **100%** — 604 statements, 152 branches, the same figures as M10a, M10b
and M10c. `git diff main -- sentinel/risk/ sentinel/analyst/prompts/fable_v1.md
sentinel/analyst/prompts/screener_v1.md screener_v2.md` **empty**.

**Goldens: 33 of 37 byte-identical. Four moved, one line each, all the same change** —
see §2. Read that before anything else.

### The three things you should know before reading further

1. **This was not a config flip.** Five defects were found in code this repository
   already described as finished, and every one of them was invisible while forex was
   disabled. Two of them — the dead tracker job and the empty history block — would each
   on its own have made forex produce nothing at all, silently, with `/health` green.
2. **The cost is lower than the plan said and the rails are the ones you chose.**
   Measured: **$0.734 per cycle of three pairs**, $8.81 on a trading day at the twelve
   cycles D2 settled on. Your original `global $20 / forex $16` was right; my "you need
   22" was computed from an estimate. §8 is the lesson in that.
3. **The observation window has an end date written into the code.** Review
   **2026-09-04**. The revert values are in `config.yaml` beside every raised number and
   asserted in `tests/test_config.py::test_the_observation_windows_rails_are_the_committed_ones`.

### Owner decisions taken on the handoff (2026-08-21)

- **D1 — the four `/pulse` golden lines are ACCEPTED.** `of $10` → `of $20` is the
  ceiling appearing where it should. See §2.
- **D2 — `scan_hours_utc` is `[7, 19)`, not `[7, 21)`.** Owner's call, reversing his own
  earlier number on the strength of §1d: 19:00–20:00 buys the day's worst evidence at
  full price and no rail catches it. 19 invents nothing — it is already
  `friday_signal_cutoff_hour_utc`. **Twelve cycles, ~$8.81 a trading day.** Global stays
  at 20.
- **D3 — the estimate chain is written up as its own lesson.** §8.
- **D4 — the spread-rail gap is recorded as FOREX.md defect #30**, with §5.3 corrected
  in place, and deferred deliberately. §1d.

### The next two actions, in order

1. **Merge and deploy** per `docs/DEPLOY.md` §13b, which is rewritten for this release.
   It is a merge and a rebuild — **no migration** — plus one browser login on your Mac
   that must be done in a single sitting.
2. **Watch for the five job ids** in the boot log, then wait for the top of the next hour
   inside 07:00–19:00 UTC.

**If anything goes wrong, `/pause forex`.** It needs no deploy, it now stops forex
*spending* as well as publishing, and it leaves the crypto measurement running. Do not
use the bare `/pause` during this window — it ends the crypto sample it was not aimed at.

---

## 1. What was measured, not assumed

Everything in this section is a reading, and each says what it is a reading *of*.

### 1a. The forex analyst prompt — join 4, closed

`python -m sentinel.tools.forex_prompt_cost --call`, `claude-fable-5`, effort high,
**against a synthetic-venue payload** (`tests/fixtures/forex_snapshot_EURUSD.json`,
assembled by the real `assemble_forex` over `tests/core/saxo_double.SyntheticSaxo`):

| | |
|---|---|
| verdict | **NO_SETUP**, schema-valid JSON on the first attempt, no retry |
| latency | **35.6 s** (37.0 s on a first run) against a 150 s timeout |
| input tokens | 10,360 + 4,854 cache read = **15,214** |
| output tokens | **2,353** |
| **cost, cached** | **$0.226104** |
| **cost, uncached** | **$0.281925** |
| **per cycle, 3 pairs** | **$0.734** — one cache write, two reads |
| input tokens, `count_tokens` alone | 13,066 → $0.1307 |

**Two things this is not.** The *verdict* is the model's answer to a synthetic market: it
is not evidence about EURUSD, and one call is not a NO_SETUP **rate** — that needs many
real cycles and arrives in the first hours after deploy. The token counts are honest to
within the digit-count difference between synthetic and real prices, a fraction of a
percent of a ~13k payload.

**Why no Saxo login was needed for this.** The cost of a call is a fact about the
payload — a system prompt, a rendered snapshot and three 1600×1000 PNGs — not about
whether the prices in it are real. The fixture carries a `_provenance` header saying so
in the file, and the tool refuses to read a snapshot that has none.

**What the plan got wrong, and by how much.** The estimate was ~$0.33/call and ~$1.00 per
cycle. Measured is $0.226/$0.282 and **$0.734** — 26% low, because **prompt caching was
not modelled**: the system block carries `cache_control`, so a cycle pays one cache write
at $12.5/Mtok and two reads at $1/Mtok rather than three full input prices.

### 1b. The spend arithmetic, over the shipped numbers

`evaluate_market_spend` suspends forex at
`global_day >= global_limit - max(0, crypto_floor - crypto_spent)`. Substituting
`global_day = crypto + forex` **cancels crypto out** while crypto is under its floor:

> **forex's real ceiling is `global_limit - 8`, not its own sub-budget.**

| global | forex's real ceiling | funds 12 cycles at $0.734 = $8.81? |
|---|---|---|
| 11 (before) | **$3.00** | no — 3 cycles of 24, all 00:00–03:00 UTC |
| **20 (now)** | **$12.00** | **yes** — 12 cycles at $0.734 is $8.81, $3.19 spare |

At 11, the UTC-midnight reset meant forex would have spent its whole allowance in the
thin Tokyo session and then gone dark through London and New York — silently, because the
spend guard suspends analysis rather than erroring.

Above the floor the reserve is spent and the plain global ceiling takes over, tightening
from there. Both halves are asserted in
`tests/core/test_forex_spend_guards.py::test_forexs_real_ceiling_is_the_global_limit_minus_cryptos_floor`.

At twelve cycles an ordinary day is **$12.42** (crypto $3.61 + forex $8.81) against a
global warn at 16 and a ceiling at 20 — roughly **$300/month** against ~$110 today, and
**~$138** for the two-week window itself.

**Crypto's floor still does its job**, on the worst possible forex day: forex can never
push the global total past `global - floor + crypto_spent`, so crypto always keeps its
full **$8** of headroom. Asserted, not argued.

### 1c. Why forex needs so much for three symbols when crypto needs $3.61 for ten

Your reading was sharper than mine and it is the finding, not the rail:

> **Forex is shipping the pre-M8.2 cost structure.**

Crypto has M8.2's two cost controls. Forex has neither.

* **No screener.** Every pair buys a full `claude-fable-5` call every cycle. Deferred —
  a new LLM stage tuned against no data would be the largest untested thing in this
  release, and crypto's screener was written *after* there was data.
* **No usable re-analysis cooldown.** You asked me to answer this plainly, so:
  **forex's setup timeframe is 1h.** `config.features.primary_timeframe` is a single
  global `"1h"` that forex shares; `fable_forex_v1.md`'s protocol puts setup structure on
  the **1h** chart; `fx/ladder.py` collapses on `0.5 × ATR(1h)` and `fx/gate.py` bounds
  the stop against `atr_1h`. One setup-TF candle is 60 minutes and the scan interval is
  60 minutes, so the cooldown would expire exactly when the next scan fires. **Saving:
  $0.00.**

  The machinery is genuine reuse — `_recently_analysed()` and `_pre_analyst_guard()` are
  already market-scoped and `SkipReason.RECENTLY_ANALYSED` already has `/pulse` wording,
  about five lines to wire. **It is not wired**, and the reason is FOREX.md defect #21's
  own words: *a rail that cannot fire is worse than an absent one, because it reads on a
  checklist as a rail.* Making it fire needs a window longer than one candle, which is a
  new uncalibrated guess. Left as the review's first question, with the window as the
  only open number.

  **I cannot re-price it from a measurement**, and that is the honest answer to your
  question 3: the saving is entirely a function of the forex NO_SETUP **rate**, and one
  call is one verdict. Any number here would be crypto's 61%-WATCHLIST figure wearing a
  forex label.

* **What is wired instead:** `forex.scan_hours_utc: [7, 19]` — a spend rail wearing a
  clock, costing no information the prompt does not already discount, since
  `fable_forex_v1.md` tells the model in as many words that a thin Tokyo session is
  weaker evidence than the London-NY overlap. Outside the window the cycle returns
  **before the first fetch**, so those hours cost zero rather than cheap.

### 1d. The 19:00–21:00 interaction — you asked me to confirm it, and the answer is bad

**The spread rail does not catch those two cycles.** `spread_gate` reaches its clock
backstop **only when the series is unusable**; the 1h tail is 1200 bars every cycle, so
`ROLLOVER_WINDOW` is effectively unreachable in production and the measured rail decides:

| | EURUSD | GBPUSD | USDJPY |
|---|---|---|---|
| threshold (3.0 × global median) | **3.3** | **5.4** | **4.5** |
| spike median at 19:00 | 1.1 passes | 1.8 passes | 1.6 passes |
| spike median at 20:00 | 1.5 passes | 4.6 **passes** | 1.9 passes |
| spike median at 21:00 | 2.7 passes | 12.0 rejects | 4.2 passes |

So on a typical evening bar nothing is rejected at 19:00 or 20:00, and 21:00 — the one
hour that does bite, and only for GBPUSD — was outside the half-open window in either
form. Those two cycles pay **full price, ~$1.47/day**, for the evening's worst
evidence, and the cost model then charges them the hour-of-day median at row 9 so most of
what they buy is rejected on net RR. Same shape as defect #15 one level out: a rail that
reads as covering the rollover window and does not.

**Owner decision D2: the window ends at 19.** Reversing his own earlier number on this
evidence, and for the evidence rather than for the money — the ~$1.47/day is the
secondary benefit. 19 invents nothing: it is already `friday_signal_cutoff_hour_utc`,
chosen for the same reason, so the daily window now ends where the Friday one does.

**Owner decision D4: the gap itself is a defect, not just an answer.** It is
**FOREX.md #30**, with §5.3 corrected in place and the full table beside it, and it is
**deferred deliberately**: every candidate fix — a lower multiple, a second per-hour
trigger, an absolute pip cap — is a new uncalibrated threshold, which is exactly what
DRY_RUN calibration exists to avoid setting blind. D2 sidesteps the window instead, so
the *exposure* is closed for the observation period and the *defect* is not.

Two tests hold the line. `test_the_clock_backstop_never_fires_once_the_series_is_usable`
pins the unreachability and says in its own docstring that a future fix must **invert**
it rather than delete it. `test_the_scan_window_ends_before_the_hours_the_rail_cannot_cover`
pins `scan_hours_utc[1] <= rollover_window_start_hour_utc`, so the exposure cannot be
re-acquired by widening the window alone.

### 1e. The kill switch — your blocking question, answered before building

**`/pause forex` exists and needs no build.** M10a shipped it: it writes a `market_pauses`
row, `/resume forex` lifts it, `core/pauses.py` composes four scopes widest-wins, and
`/pause` with no argument stays system-wide on purpose. Crypto is untouched by it.

**But it was the wrong kind of switch on the forex path.** The crypto cycle checks
`_paused()` before the analyst (`orchestrator.py:822`, with the reason in a comment:
*"analysing first would buy a ~$0.32 rejection"*). `_run_forex_cycle` read the pause only
inside the per-user fan-out, **after three analyst calls** — so `/pause forex` cost
**~$0.73 per cycle to be paused**, and cost is the first reason on your list for reaching
for it. Fixed as part of step 5; asserted on the LLM call count, not on the absence of a
card, because "no card was published" was already true of the broken version.

`docs/DEPLOY.md` §13b now carries the four-lever table with the warning about bare
`/pause`.

---

## 2. The goldens — exactly what moved

**33 of 37 byte-identical. Four files moved, one line each, and every one is the same
change:**

```
- 💵 <b>~$0.31 this cycle</b> · $7.42 today of $10 <i>(estimate, not a bill)</i>
+ 💵 <b>~$0.31 this cycle</b> · $7.42 today of $20 <i>(estimate, not a bill)</i>
```

`surfaces/pulse.txt`, `surfaces/pulse_24h.txt`, and both `surfaces_multi/` twins.

**Cause.** `/pulse` renders the deployment-wide spend limit, and this release raises it
from 10 to 20 for the observation window. **Leaving `llm.daily_spend_limit_usd` at 10 was
tried and is worse:** `/pulse`'s state comes from `evaluate_spend` against that same
number, so an ordinary $13.89 day would have shown **LIMIT_REACHED on `/pulse` every
single day** while the system ran perfectly.

**Nothing moved for the market tag**, which is what M10c pre-pinned and it held exactly.
`card.txt`, `prompt.txt`, both gate JSONs, all three features and charts sets, `status`,
`stats`, `positions`, `watchlist`, `snapshot`, `pulse_symbol` and both journals are
untouched.

### 2b. What M10c's "switch-on moves no golden" missed (spec defect #28)

The claim was half right, and the half it missed cost 47 test failures at the flip.

M10c pinned the tagged **bytes** in advance — `card_multi.txt` and `surfaces_multi/` —
so no fixture's *contents* had to change for the tag. What it could not pin is that
`tests/golden/surfaces.render_surfaces()` **defaults to `load_config()`**. With forex
enabled the shipped config renders the *tagged* form, so six untagged goldens plus the
journal were being compared against it, and
`test_every_multi_market_surface_differs_by_its_header_alone` went vacuous because
`single` and `multi` had become the same config.

**The mapping moved, not the bytes.** `single_market()` now mirrors `multi_market()`; the
single-market set renders from it and the multi-market set from the shipped config, and
both relationships stay pinned.

### 2c. Three real findings inside those 47 failures

Each was a test that looked like coverage and was not.

1. **`tests/golden/surfaces._spend()` ignored its own config**, calling `load_config()`
   directly, so `test_the_deployed_config_renders_the_same_surface` was **blind to the
   entire `llm:` block** on both sides. Found only because raising the ceiling moved
   `/pulse` and the legacy comparison did not notice.
2. **`FakeSignalRepository._mine()` filtered by user and never by market**, while the real
   `SignalRepository` is market-scoped. `/positions` iterates the enabled markets and
   rehydrates each row against the market it is iterating (§16.7), so the forex pass got
   crypto rows, raised 45 validation errors and the command answered *"something went
   wrong"*. Exactly the gap M10c §13 closed for the real repository against Postgres:
   **a missing market filter also passes when the table holds one market's rows.**
3. **`generate_goldens_m10a` regenerated the single-market set from the shipped config**
   and silently rewrote six files in the tagged form. Only the sha256 manifest noticed.
   It now takes `single_market(config)` and says why.

### 2d. One thing I edited and reverted, recorded because the reasoning matters

I raised `daily_spend_limit_usd` in `tests/fixtures/config_legacy_deployed.yaml` to keep
the shape comparison passing, then reverted it. **In a `markets:`-less config that key
*is* crypto's budget** — `normalise_markets` synthesises
`markets.crypto.llm_daily_budget_usd` from it — so raising it there asserts crypto's
budget doubled, which it has not.

The real situation is that the legacy shape **cannot express** a deployment ceiling
distinct from the single market's budget; that key did not exist pre-M10a. Once the two
legitimately differ, the two shapes cannot render the same deployment-wide spend line.
`as_legacy_settings()` holds that one setting equal so the test stays a statement about
**shape**, which is what it claims to be.

---

## 3. The five defects, and why every one was invisible

All five were in code this repository already described as done. **Four of the five were
found by composing a join or by turning the flag on — none by reading.**

### 3a. Forex would have been dead on its first cycle (three ways)

| what | what would have happened |
|---|---|
| `bootstrap()` called from nowhere | `SAXO_REFRESH_TOKEN` never left `.env`; empty store at boot; `refresh()` raises *"no Saxo credential is stored"* — **dead on cycle one** with a valid credential a metre away |
| `ensure_fresh()` called from nowhere | the token touched once an hour by a scan, against a credential that lives about an hour — **survival by luck** |
| `ReauthenticationRequired` caught nowhere that could send a message | **silence** |
| `app.track_for` → `market_adapter` for forex | `UnknownAdapter` on **every 60-second tick, for ever**, swallowed into a log line, `/health` green |
| `analyze(snapshot, charts, "")` | an empty text block is an **HTTP 400** — *every* forex analyst call fails, as an `AnalystUnavailable` that reads like a vendor problem |

Every one of them produces **no error a human would see**. That is not a coincidence:
they are all instances of HANDOFF §4 item 1 — a path with no positive reachability test
on real machinery.

### 3b. The 400 is the one worth dwelling on

`_analyse_forex_symbol` passed a literal `""` as its history block, and `user_blocks`
appended it unconditionally. Verified against the live endpoint **before** writing the
fix:

```
WITH an empty text block     -> 400: messages: text content blocks must be non-empty
WITHOUT one                  -> OK, 8 input tokens
```

Crypto is safe by accident of content: `build_history_block` and `news_block` both always
emit their headers, so neither has ever been empty. Fixed in **both** places — the forex
path now builds a real history block through the same market-scoped `_history_block`
crypto uses, and `_append_text` makes the failure impossible rather than merely absent.

Nothing could have caught this except sending the prompt, which is join 4, which M10c
named and left open.

### 3c. What the tracker join actually settled

Predicted three things. **One was wrong, and it was the loudest.**

* **CONFIRMED — the invalidation read was an hour behind.** Saxo has no closed flag, so
  the adapter drops the forming bar; `PriceFeed` then drops the last row *again* because
  on Binance that row is the forming one. Two drops, one bar, and the discarded bar is
  the newest closed hour — the only hour an invalidation is ever measured on.
* **CONFIRMED and quantified — the fill read was 120 s stale on every tick.**
  `now >= T + H + grace` with a 1-minute horizon and 30 s of grace. M7 reads 1m high/low
  precisely so a 60-second poll cannot miss the wick that filled a rung.
* **DISPROVED — a weekend gap is not a degraded read.** I expected `fetch_tail`'s
  `len(rows) == limit` assertion to fail every Monday morning. It does not: no anchor is
  sent (D-d), so the venue returns the newest `Count` bars and walks back through the
  gap — 1198 of a requested 1200 at Monday 08:05 after a Friday 18:00 last tick, the two
  missing ones being the forming bars. Kept as a test, because it is the expensive case.

`sentinel/fx/pricing.ForexCandleSource` answers each of `PriceFeed`'s two questions
separately. `sentinel/tracker/` and the crypto path are untouched. `is_closed` is now
public on the adapter because it has two callers and must never have two implementations
— a private copy could disagree at the 30-second grace boundary, and the disagreement
would invalidate a signal on an open bar.

### 3d. And one in `/journal`

`JournalRow.leverage` is `int | None`, and M10c fed it `leverage_of`, whose forex answer
is the **string** `"30x max"`. Crypto's `"5"` coerced silently; forex raised a
`ValidationError` and **took `/journal` down for the whole workbook** — every population,
not just the forex rows.

`journal_leverage_of` returns the per-position figure or `None`, and **`None` for forex is
the honest answer rather than the convenient one**: §7.6's margin is account-level and a
30:1 cap repeated identically on every row is not a per-position quantity. The cap keeps
its ESMA words on the card, where it means something. Both halves are pinned in one test
so a future fix cannot satisfy either alone.

---

## 4. The calendar

Eleven events, 2026-09-01 to 2026-10-30, every one from the issuing institution. Four
additions to the loader's shape, each because a marker nobody can check is worse than no
marker:

| field | why |
|---|---|
| `source` **required** | a wrong date is a blackout that never fires, and silence is the failure this system cannot see. A URL makes a date re-checkable in ten seconds. A field that may be omitted is a field that will be. |
| `time_confidence: approximate` + per-event window | BoJ publishes no announcement time — *"the afternoon of the second day"*, observed 02:30–06:00 UTC. Both BoJ entries carry **−180/+60** against the configured −60/+30. **The loader refuses `approximate` without an explicit window**, so the marker cannot become decoration, and a test asserts the window is *wider than the config default* rather than equal to 180. |
| `needs_verification` | both ECB dates come from agreeing secondary sources, not the ECB's own Governing Council page. Changes no behaviour; changes what the alert asks for. |
| file-level `known_gaps` | **US PCE is absent.** bea.gov could not be reached and no date was guessed. |

**On `coverage_until` and PCE.** `coverage_until` alone is a claim of completeness and
this file cannot honestly make one. Claiming no coverage suppresses every forex signal
and would have made the milestone pointless. So the claim is **"complete except these"**:
`known_gaps` travels with the coverage date and the alert repeats it until it is filled
in. That is the compromise, stated rather than hidden.

**BoE has no October meeting.** The next Bank Rate decision after 17 September is
5 November, outside this window. Recorded in the file so a later reader does not read it
as a gap.

**No DST boundary falls inside the window** — September and October 2026 are entirely
inside EDT/BST/CEST, so every instant is a plain conversion. The next boundary is outside
it, which is a second reason coverage stops on the 30th.

### 4b. The staleness rail finally says something

**The rail was built at M10b and read by nobody.** `health()` was called only from inside
`blackout()`, so its `warn_within_days` branch composed a sentence that never left the
process. The calendar could run out, the gate would start rejecting every symbol with
`CALENDAR_STALE`, and the first sign would be that no forex card had arrived for a while.

The distinction you asked about **already existed and was already correct**, and I did not
change it: *"no events today"* → allowed; *"the calendar has run out"* → `CALENDAR_STALE`
→ **blocks**. With one source, "I do not know" and "nothing is scheduled" are the same
silence and only one is safe to trade through. What was missing was the message.

Now: `AlertKind.FOREX_CALENDAR_COVERAGE`, fired at the **top** of the forex cycle before
the fetch, **once per UTC day** through the existing claim-and-confirm notice path — 24
cycles for a fortnight is 336 messages, which is a message nobody reads, and an ignored
rail is the silence it exists to prevent.

**Two states, two messages**, because they have different outcomes: *expiring* is a
warning with signals still flowing, *expired* means forex is emitting nothing. A test
pins that the texts **differ** rather than pinning either literal.

`calendar_warn_within_days: 14` stands. Two weeks of daily nudges for ~6–8 dates across
four institutions; the burden is research, not typing.

**The reachability test is the point of the file.** Nothing in
`tests/core/test_forex_calendar_alert.py` asserts a return value. Each test drives the
real orchestrator method, the real alert builder, the real read model, the real card
renderer and a real `UserNotifier`, then reads the **text in the bot's outbox**. Teeth
proved by making the alerter never fire: 7 of 8 fail, and the eighth is the
"a healthy calendar sends nothing" sibling that has to keep passing.

---

## 5. Numbers

* **2132 passed, 68 skipped, 0 failed** — up from M10c's 2076/68, **56 new tests**.

  | file | tests |
  |---|---|
  | `tests/core/test_forex_calendar_alert.py` | 8 (new) |
  | `tests/core/test_forex_reauth_alert.py` | 9 (new) |
  | `tests/core/test_forex_spend_guards.py` | 8 (new) |
  | `tests/core/test_forex_tracker_tick.py` | 6 (new) |
  | `tests/bot/test_forex_surfaces.py` | 5 (new) |
  | `tests/bot/test_publisher.py` | +3 |
  | `tests/analyst/test_provider.py` | +2 |
  | `tests/fx/test_calendar.py` | +5 |
  | `tests/fx/test_spread.py` | +3 (defect #30) |

* `make check` **exit 0**: tests, ruff, `mypy --strict` over 310 files, 100% risk branch
  coverage (604 statements, 152 branches — unchanged since M10a), `check-deps`,
  `check-ops`, wheel build, image build, in-image import — which confirms both analyst
  prompts **and the now-populated calendar** ship — and `pins agree: 7 pinned
  dependencies identical in .venv and the image`.
* **`sentinel/risk/` and the three crypto prompts: zero-line diff.**
* Goldens: **37 total, 33 byte-identical, 4 moved by one line each** (§2).
* 52 files changed. **No migration.**
* Eight commits, one per step.
* **Two real Anthropic calls were made**, both by `sentinel.tools.forex_prompt_cost`,
  ~$0.45 total. The first printed the wrong attribute name after a successful call and
  had to be repeated.

---

## 6. What is still NOT verified

**No forex cycle has ever run against the real Saxo API.** Every measurement in §1 is
against `SyntheticSaxo`. The joins compose; that is not evidence about the market.

**No forex card has reached a real Telegram chat.** The publisher's dispatch, its claim
and its double-press no-op are now proven for forex against fakes. Real delivery is a
deploy-time check, in `docs/DEPLOY.md` §13b step 4.

**The NO_SETUP rate is unknown.** One call is one verdict, and that verdict was about a
synthetic market. The rate arrives in the first hours after switch-on and it is the number
the screener decision at the review depends on.

**Every threshold added at M10c is still an uncalibrated guess**, and so is
`scan_hours_utc`. So is `spread_max_multiple: 3.0` — and §1d shows it does not fire in the
hours it was written for.

**The swap table is still empty and commission is still zero.** Any net-RR figure prices
the spread and nothing else; a position held overnight will cost more than the card says.

**€200 still cannot trade this market.** `--capital 200` on a 35-pip stop returns
`BELOW_MIN_TICKET`, unchanged, and it is run as a control.

**The re-auth chain has never been exercised against real Saxo.** The alert is proven
against a scripted 400. Whether Saxo returns a 4xx in the shape the code expects when a
refresh token genuinely expires is a deploy-time observation.

### The joins this milestone's own boundary leaves untested

The rule from M10b-2 §2, applied to myself:

1. **No forex signal has been tracked to an outcome.** The tick runs, prices a real Saxo
   1m tail and advances the state machine — but no forex position has filled, hit a
   target or stopped out, so `fx/accounting.realized_costs_eur` has never priced a real
   one.
2. **The credential has never actually rotated in production.** `refresh_rotated=True`
   is asserted against a scripted response.
3. **`/journal` has never exported a forex row from Postgres.** It is proven from a
   detached row through the real builder and the real workbook writer.
4. **The calendar alert has never been rendered by real Telegram.** `assert_sendable`
   covers the HTML; the send is a fake.

---

## 7. Open questions for the 2026-09-04 review

In the order I would ask them.

1. **Do the rails come back down?** Revert values are in `config.yaml` and in
   `test_the_observation_windows_rails_are_the_committed_ones`. If forex continues, they
   are re-justified from measured spend rather than left standing.
2. **Does defect #30 get fixed, or does the window keep sidestepping it?** D2 closed the
   exposure by ending the scan at 19; the rail that cannot fire is still there. Fixing it
   needs a calibrated threshold, which is what this window is for producing.
3. **A forex screener?** Now answerable with a measured NO_SETUP rate. If it is high, a
   $0.023 sonnet pass in front of a $0.23 call is the largest lever available.
4. **A re-analysis cooldown longer than one candle?** Only worth it with the same rate in
   hand, and only as a deliberate trade — a >1-candle window discards a real bar.
5. **The two ECB dates and US PCE.** The alert will keep asking. They are the only
   knowingly-incomplete part of the calendar.
6. **Does `/pulse` display the right limit?** It prints `llm.daily_spend_limit_usd`, but
   the operative deployment ceiling is `llm_daily_budget_global_usd`. They have disagreed
   since M10a (10 against 11) and nobody noticed because the numbers were close. This
   release keeps them equal at 20 rather than fixing the field, because changing which
   field a crypto card reads is beyond a switch-on. It is a latent defect and it is yours
   to schedule.

---

## 8. The lesson: a cost model is not a measurement (owner requirement D3)

**Four estimates of the same number, by two people, all wrong in the same direction. One
measurement settled it.**

| | figure | basis |
|---|---|---|
| owner, in the brief | "roughly **$2/day** of room" | the shipped rails, read informally |
| me, from the code | **$3.00/day** exactly | correct arithmetic on `evaluate_market_spend` |
| me, per call | **~$0.33** | system prompt chars + payload chars + 3 charts × 2,130 visual tokens, at list price |
| me, per cycle | **~$1.00** | three times the above |
| me, per day | **~$24** | 24 cycles × the above |
| **measured** | **$0.226104 cached / $0.281925 uncached, $0.734 per cycle** | one real call |

The per-day estimate was **27% high**. The $3.00 was right, and worth separating: that
one was *arithmetic over values the code already holds*, which is a different kind of
claim again and is the only one of the five that survived contact.

**What the estimate missed was not a token count.** The character counts were close and
the chart figure was close. It missed a **mechanism**: the system block carries
`cache_control`, so a cycle of three pairs pays one cache write at $12.5/Mtok and two
reads at $1/Mtok instead of three full inputs at $10/Mtok. Nothing in a token count and a
price list can tell you that. It is a fact about how the call is made, and the only place
it is visible is a response's `usage` block.

**The direction matters as much as the size.** Every estimate erred *high*, and a high
cost estimate argues for a **higher spend rail**. Acting on it, we would have set the
global ceiling to 22 and given a runaway loop $2/day more blast radius than the measured
need justifies — a permanent widening bought with an imaginary number. The rail is the
last thing standing between a bug and the bill; that is a bad place for a 27% error, in
either direction but especially that one.

### The rule

> **Price an LLM path from a real call before setting a rail on it.** A cost model built
> from token counts and list prices is a *different kind of claim* from a measured call,
> and the two must not be mixed in one sentence. When only an estimate is available, say
> so and set the rail after the first measurement rather than before.

It is cheap to obey. The measurement here cost **$0.45 and about twenty minutes**, and it
had to be built anyway — join 4 named "no token count, no cost figure" as an open item at
both M10b-2 and M10c and it was carried forward twice. The estimate was not a shortcut
around the measurement; it was work done *instead of* the measurement that then had to be
thrown away.

### The family it belongs to

This is HANDOFF §4's recurring shape in a new place. Items 10, 11 and 12 are all *"a
check that looked like the thing it stood in for"* — a golden that pinned one branch, a
suite that proved every piece works alone, a fixture that could not produce the input
that breaks. An estimate is the same: it looks like a figure, it goes into a table
alongside measured figures, and nothing about its rendering says it was derived rather
than observed. In this report §1's table says which is which in its own header, and
`sentinel/tools/forex_prompt_cost.py` prints the caveat with every number it produces —
including the two it *under*-states and why.
