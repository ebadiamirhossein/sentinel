# M10c — the forex plan, the card, publishing and tracking

**Date:** 2026-08-21. **Status:** complete, `make check` **exit 0**, **not deployed.**

The milestone that answers spec defect #12. `TradePlan` lives in the frozen
`sentinel/risk/` and is crypto-shaped, so M10b had to stop a forex cycle at a stored
`analyst_reports` row. This gives a forex signal a shape to travel in, renders it,
publishes it idempotently and tracks it in a market that is shut 49 hours a week.

`markets.forex.enabled` is still `false`. **Deploying this is a no-op** — §11 says what
that rests on.

**Read §3 and §5 first.** One is a live defect on a card the owner reads today; the
other is a defect in the milestone brief itself.

---

## 1. The binding constraint held

```
$ git diff --stat sentinel/risk/ sentinel/analyst/prompts/fable_v1.md \
                  sentinel/analyst/prompts/screener_v1.md sentinel/analyst/prompts/screener_v2.md
(no output)
```

Risk-engine branch coverage unchanged at **100%** — 604 statements, 152 branches, the
same figures as M10a, M10b-1 and M10b-2.

Golden fixtures were sha256-checked **before the first edit and after every step**, by a
guard that distinguishes an *addition* from a *regeneration* because they are different
events:

| step | goldens |
|---|---|
| 1 numpy pin | 27 byte-identical |
| 2 spec §16 | 27 byte-identical |
| 3 thresholds | 27 byte-identical |
| 4–6 plan, gate, card | 27 byte-identical |
| 7 publish, track, orchestrate | 27 byte-identical |
| **9 the deliberate regeneration** | **2 modified, approved in advance** |
| 9b multi-market | **27 byte-identical, 10 added** |
| 10 degradation | 37 byte-identical |

---

## 2. The design question, and how it was answered

FOREX.md **§16** is new and was written **before** any of the code it describes — the
milestone writes its own spec section, deliberately, because M10b-1 proved this part
could not be specified accurately without reading `sentinel/risk/models.py` first.

`ForexPlan` is a **parallel, independently-defined model**. Not a `TradePlan` (five
crypto-only fields plus six `funding_*`), not a subclass (inherits them), not a shared
base class (requires editing the frozen package). It mirrors `TradePlan`'s shared
vocabulary field for field, adds what forex genuinely has, and has **no field at all**
for a liquidation buffer, a funding rate, a USDT notional or a derived leverage —
asserted by a test, not intended by a comment.

**The mirroring is load-bearing, and it paid for itself five times.** These functions
now serve both markets and **their bodies did not change**:

* `storage.repositories.signal_row` — so one `signals` table takes both, **no migration**
* `GateDecisionRepository.to_row` — so `/pulse` groups both markets' verdicts
* `bot.readmodels.position_view` — so `/positions` renders either
* `tracker.detect.observe` and `tracker.machine.advance` — one detection path, one state
  machine

The three places the markets genuinely differ went into `bot/plans.py` as named
accessors with the reason attached: `eur_quote_rate_of` (EURJPY on the yen cross),
`qty_step_of` (Saxo's `AmountDecimals` vs Binance's step) and `leverage_of` (a *derived
suggestion* against a *configured ESMA cap* — different kinds of thing, and a card must
not read as though they were not).

The gate is a **composition**, not new arithmetic: every rail already existed as a pure
verdict function in `sentinel/fx/`. What `fx/gate.py` adds is the order — cheap
market-condition rails first, net RR last so the harder blocker always wins.

### Two reversals of my own spec section, both recorded in the code

**§16.2 said `ForexEntryRung.units`. It is `qty`.** The danger `units` guards against is
a units figure in a *money* slot, and `qty` is not a money slot. What it would actually
have bought is a rename at `signal_fills.qty`, `risk.accounting.Fill`,
`SignalTracking.filled_qty` and the whole detection path — the translation layer §16.2
exists to avoid, bought with nothing.

**§16.5 said nothing imports `sentinel/risk/`. One thing does:**
`management_plan_text`, because both markets are managed by the *same* config block and
two copies would drift while one block claimed to set both.

---

## 3. A live defect on a card the owner reads today (spec defect #22)

The brief asked for `capital` to render as `€200.00`. It is worth being precise about
why it did not, because the cause outlives the fix.

`capital_eur` and `risk_per_trade_pct` are the **only two** `TradePlan` fields the engine
assigns without `money()` or `percent()`, and both originate in `Numeric(38, 18)`
columns. Postgres returns `Decimal('200.000000000000000000')` and the card prints it.
The live signal card has read

```
🎯 Plan (capital €200.000000000000000000 · risk 0.750000000000000000% = €1.50 · EURUSD 1.1593)
```

for 54 cycles.

**Fixed at the render seam, not at the source.** The architecturally correct place is
`risk/engine.py`; that package is frozen for the live measurement window, and a
display-scale fix is not worth spending the freeze on. `bot/formatting` gained
`money_eur` and `percent_2dp`.

### The audit found two more, still live

Asked which *other* surfaces have the same blind spot, `/status` turned out to print

```
  risk per trade: 0.750000000000000000%
  open risk: 1.500000000000000000% of 2.25%
```

— the same defect, two lines below the capital, on the card I had just "fixed". Both go
through `percent_2dp`, which is **fixed** at two decimals rather than trimmed precisely
so a scale fix cannot smuggle a convention change in behind it: **no further golden byte
moved.**

### Why no golden could see it, and what still cannot

`tests/golden/pipeline.py` sizes against `Decimal("10000")` constructed in Python. The
live system reads a `Numeric(38, 18)` column. The two differ in **scale, not value**, so
the fixture structurally cannot produce the input that breaks — and no amount of *more*
golden coverage would have found it.

**The remaining exposure, named:** every value on the cycle golden except those two is
quantized by `sentinel/risk/` before it reaches a card, so scale is pinned upstream and
the fixture's provenance does not matter. The **surfaces** goldens are the exception —
`StatusView`, `SettingsView` and `UserView` carry raw `Decimal`s straight from repository
rows, and `tests/golden/surfaces.py` builds those rows in memory too. Nothing there is
currently wrong, and nothing there would show it if it became wrong. Recorded as
HANDOFF §4 item 12.

### The guard that should have caught it, and now does

`test_no_arithmetic`'s layer 2 compared rendered numbers to plan numbers **as strings** —
and `str(Decimal("200.000000000000000000"))` *is* what the card printed, so it passed
with the defect in front of it. **A test that looks like it is checking and is not is
worse than no test**, because it consumes the attention that would have found the gap:
the file reads like a thorough guard, it already had a proof-of-teeth test, and it was
blind to an entire class of defect.

It now compares by **value**, with an explicit third layer stating the rule that was
missing — *the renderer may fix a display scale; it may never change a value* — and its
proof of teeth perturbs €4570.30 to €4570.**31**: one digit, same scale, so it fails
unless the value comparison genuinely works. Recorded as HANDOFF §4 item 13, because the
pattern generalises: **when a check is loosened, the new test must break on the smallest
thing the check still has to catch.**

---

## 4. The regeneration, and the file that was missing from the brief

Three artefacts changed, by **exactly one line each**, and only after explicit approval:

| file | before | after |
|---|---|---|
| `tests/fixtures/golden_cycle/card.txt` | `capital €10000` | `capital €10000.00` |
| `…/surfaces/status.txt` | `  capital: €10000` | `  capital: €10000.00` |
| `docs/specs/TELEGRAM_UX.md` §1 | `capital €10000` | `capital €10000.00` |

The other **25 were re-hashed and shown byte-identical** — including
`gate_approved.json`, which still carries `"capital_eur": "10000"`, because the *plan*
did not change. Only the rendering did.

**The third file was not in the brief.** `docs/specs/TELEGRAM_UX.md` §1 is generated by
`tools/signal --doc` and pinned by a test, so it is a golden in everything but name;
regenerating the other two without it would have left the spec disagreeing with the code
— the exact drift that test was added in M6 to stop. Raised before regenerating, and the
owner asked for it to be recorded here as an **owner-instruction gap** rather than
quietly patched. It is one.

---

## 5. Defect #23 — a defect in the milestone instruction, not in the spec

The brief required §11's regeneration to happen in this milestone, because switching
forex on gives crypto cards their market tag and changes their bytes. It also required
`markets.forex.enabled: false`. **Both cannot hold.** With one market enabled
`AppConfig.multi_market` is `False`, `section_header` returns `""`, and no crypto card
byte can move. There was nothing to regenerate.

**Owner ruling: pin the tagged form as an addition instead.** Ten new fixtures —
`card_multi.txt` and `surfaces_multi/` — rendered from the shipped config with forex
flipped on *in memory*, by their own generator with a hard write-allowlist. The shipped
config stays single-market, so `test_the_deployed_config_renders_the_same_surface` keeps
holding, and **switch-on day becomes a config flip that moves no golden at all** rather
than a regeneration performed on the one day there is least attention to spare for it.

**The load-bearing test is not a byte comparison.** A golden that merely records what
the code does today ratifies a mistake as readily as a fix. What makes these worth
having is `test_every_multi_market_surface_differs_by_its_header_alone`, which pins the
*relationship*: the tagged form is the untagged form plus a header, and nothing else.
specs/TELEGRAM_UX.md §3e has promised that since M10a and only its other half — "with
one market, every surface is unchanged" — was ever asserted. The half switch-on day
depends on was not.

**A gap it found in the existing harness.** `render_surfaces` rendered `/pulse` and
`/pulse 24h` **without** the header their handlers pass. With one market
`section_header` returns `""`, so the golden was right by coincidence — and the
multi-market golden would have under-pinned exactly the line switch-on adds to those two
cards. Fixed; both files are byte-identical, which is the check saying the fix cost
nothing.

---

## 6. Spec defect #21 — a rail that could never fire

FOREX.md §7 and §9 name the sizing rails and the correlation cap and say nothing about
setup **quality**, so the forex gate had no minimum RR, no confidence floor, no
entry-distance bound, no ATR bounds and no cooldown — and the obvious move was to read
crypto's `risk:` block.

One of those numbers is actively wrong here. `max_entry_distance_pct` is **3.0**, and
EURUSD moves about **0.5% in a day** — so a 3% bound could essentially never fire. **A
rail that cannot fire is worse than an absent one, because it reads on a checklist as a
rail.** Forex gets 0.5, and crypto's block is untouched.

The load-bearing test asserts the two are *different* rather than that forex's is 0.5,
so DRY_RUN calibration does not have to edit a test whose point is the comparison — with
a non-vacuity sibling proving the bound still admits an ordinary 12-pip pullback entry.

`min_rr_tp1` is deliberately the **same** 1.5: two markets gated at the same *net* level
is what makes their statistics comparable. The ATR stop bounds carry over unchanged
because ATR is already the instrument's own volatility, which is what makes them
transferable where a percentage is not.

---

## 7. Four joins composed before anything was built on them

M10b-2 §12 named four joins its own boundary left untested by construction.
`tests/core/test_forex_signal_path.py` was written **before** `fx/plan.py`, `fx/gate.py`
and `bot/forex_cards.py` existed. It failed on `ModuleNotFoundError`, then on 47
validation errors, then passed.

It earned its cost twice.

**`SignalRecord.plan` widening produced 27 `mypy --strict` errors**, every one a real
place that read a crypto-only field off a plan that may not have one — `cards.py` reading
`liq_buffer_ok`, the tracker reading `eurusd_rate`, the journal reading
`suggested_leverage`.

**And it caught a defect a unit test on either side would have missed — again.**
`_forex_gate_and_publish` first did what the crypto path does:
`SymbolFeatures.model_validate(snapshot.features)`. It raises. M10b-2 merged the forex
block *into* that dict so a new top-level snapshot field would not move the crypto prompt
golden, and `SymbolFeatures` is `extra="forbid"`. Both halves were tested; the join was
not. That is the third instance of this exact shape in three milestones.

---

## 8. The hazard the union creates, and what actually answers it

`SignalRecord.plan` is now `TradePlan | ForexPlan` (owner-approved contract change; **no
migration** — `market`, `plan` and `chart_params` are already columns).

Because `ForexPlan` mirrors `TradePlan` deliberately, a mis-dispatched rehydration could
produce a **plausible object** rather than an error — the pip derivation's shape exactly,
where `10 ** -(decimals - 1)` gives believable prices and no exception.

So: `plan_of` dispatches on `row.market` and there is deliberately **no**
try-then-fall-back, because a fallback is the mechanism that turns a mis-dispatch into a
plausible object. Both renderers raise `TypeError` on the other's plan. Five sites that
still called `TradePlan.model_validate(row.plan)` were swept — the journal one is worth
naming, because its `except ValidationError` blanks sizing columns for an old schema and
would silently have blanked **every forex row** in an export whose whole purpose is a
record of what happened.

**What makes it safe is not the dispatch function but the shape of the two models**, and
that is pinned separately: `test_plan_dispatch.py` asserts both directions fail loudly
**and** that neither model's field set is a subset of the other's — the property the
refusal rests on, so a future field addition cannot quietly make one plan validate as the
other.

---

## 9. Tracking a market that is shut 49 hours a week

Three behaviours with no 24/7 analogue, and the first would never have been found by
watching for errors because it produces none.

**A closed market is not a stall.** Ticking through a weekend asks a shut venue the same
question ~2,940 times, logs a capped lookback every minute because the window spans two
days, and re-walks Friday's bars as though they were news. None of it is an error — it
would simply have been the majority of what the tracker did. The test asserts on the
**feed**, not on the result, because `checked == 0` would also be true of a tick that
fetched every candle and found nothing.

**No pending ladder can rest over a weekend — enforced at gate time, not at detection
time.** `plan_expiry` is `min(TTL, Friday close)`, and a parametrized property test walks
every weekday × every TTL to prove the deadline always lands inside the trading week. A
detector subtracting closed hours 1,440 times a day would be a second implementation of
the same rule, able to disagree with the number printed on the card. The plan records
**which** deadline it was, in words, because §5.4 gives a ladder two ways to die and one
timestamp cannot say which.

**A currency-matched high-impact event cancels a pending ladder** with a reason the owner
sees. An open position is never closed — and that falls out of the state machine refusing
to expire a filled signal, rather than needing a second rule to say so.

---

## 10. Numbers

* **2065 passed, 68 skipped**, 0 failed in the hermetic suite — up from 1902/60,
  **171 new tests**. The 68 skips are the opt-in Postgres tests, and they were
  **run**: with `SENTINEL_TEST_DATABASE_URL` set against `sentinel_test` at revision
  `0011_forex_spine`, **2132 passed, 1 skipped, 0 failed** — the dev database
  untouched and the test database cleaned up behind itself.

  | file | tests |
  |---|---|
  | `tests/fx/test_gate.py` | 44 |
  | `tests/bot/test_forex_cards.py` | 23 |
  | `tests/bot/test_plan_dispatch.py` | 12 |
  | `tests/core/test_forex_signal_path.py` | 11 |
  | `tests/tracker/test_forex_tracking.py` | 11 |
  | `tests/golden/test_golden_multi_market.py` | 8 |
  | `tests/fx/test_config_thresholds.py` | 7 |
  | `tests/core/test_forex_degrades_only.py` | 16 (was 11) |
  | `tests/bot/test_no_arithmetic.py` | 10 (was 6) |
  | `tests/storage/test_forex_signal_persistence.py` | 8 (opt-in, **run**) |

* `make check` **exit 0**: tests, ruff, `mypy --strict` over **296 files**, 100% risk
  branch coverage, `check-deps`, `check-ops`, wheel build, image build and in-image
  import — which confirms **both** analyst prompts and the calendar ship.
* `sentinel/risk/` and the three crypto prompts: **zero-line diff.**
* Goldens: **27 → 2 deliberately regenerated, 25 byte-identical, 10 added.**
* 70 files changed, ~7,070 insertions. **No migration.**
* Eight commits, one per step, with the regeneration alone in its own.

---

## 11. Why deploying this is a no-op

`markets.forex.enabled` is `false`. Therefore:

- `enabled_markets` is `(crypto,)`, so exactly one `scan:crypto` job and one `tracker`
  job are registered — asserted, including that the crypto tracker keeps its bare
  `tracker` job id, because APScheduler keys on the id and the running deployment's job
  is that one.
- `CycleOrchestrator` is only ever constructed with `market=crypto`, so the forex branch
  in `_run_cycle` is unreachable.
- `config.multi_market` is still `False`, so no card gains a market tag.
- The **only** change to crypto's output is the two lines of defect #22, approved in
  advance and shown line by line before they were written.
- `test_a_crypto_cycle_never_touches_a_saxo_adapter_or_credential` still passes.
- `FxClient._fetch_live` keeps `symbols=USD` **exactly** — the request a live crypto
  cycle makes every hour. `fetch_quotes` is a new method used only by the forex path.

**No migration runs.**

---

## 12. The numpy pin, and what it found

`numpy==2.2.6`, settling journal/M10b_REPORT.md §3a's open question. It sits under
pinned matplotlib and mplfinance and under every RSI, ATR and EMA value, and it was the
one dependency in that stack still floating.

**The finding that came with it: a freshly built image resolved numpy 2.5.2 while
`.venv` resolved 2.2.6.** The suite has never once validated the numpy the container
runs. The cause is a **dev-extra** constraint — `pandas-ta` pulls `numba`, which refuses
NumPy above 2.2 — silently holding the whole test environment two minor versions behind
production. Same shape as M10b-1 §3's three-versions-of-anthropic defect, and invisible
for the same reason: every check runs where the constraint applies.

Measured rather than assumed: the golden, chart and feature suites were run in a scratch
venv at **numpy 2.5.2** and **every golden was byte-identical**; the only failure was
`pandas-ta` itself, which cannot import there. So the two agree today, and the pin makes
`.venv`, the image and the goldens name one number.

```
image before the pin: numpy 2.5.2
image after  the pin: numpy 2.2.6
```

**`pandas` is the same hole and was deliberately not touched** — it is `>=2.2`, it sits
in the same position under the chart and indicator stack, and it was not in the brief.
Its own decision.

### The pattern, not the incident (owner requirement K2)

This is the **same root cause as `httpx`, arriving from the opposite direction**. There,
a dependency production needed was declared by nobody and arrived transitively. Here, a
**test-only** package silently version-controls the numerical core of the product. The
rule is now HANDOFF §4 item 14: *a dev extra must never constrain a production
dependency's version.*

**Can `check-deps` detect this class? No, and it should not be extended to.** It reads
the *source tree* and asks a question about **declaration** — is every third-party import
a declared dependency — which it answers without resolving anything, which is what makes
it fast and hermetic. This is a question about **resolution**: do two environments
resolve the same version of a shared package. That cannot be answered without building
both, and bolting it on would make `check-fast` depend on a Docker build.

**It belongs in `check-image`**, which already builds the image and already imports the
app inside it. The check is cheap: for every *pinned* dependency, assert the version
installed in the image equals the version installed in `.venv`, and fail naming both.
That catches this defect **and** M10b-1 §3's three-versions-of-anthropic defect with one
assertion, at build time in a gate rather than at boot on the server. **Not built in
M10c** — named here and in HANDOFF so it is a decision rather than an oversight.

---

## 13. What is NOT verified — and what my own boundary leaves untested

**No live forex cycle has ever run.** Everything here is exercised against
`tests/core/saxo_double.py`, a synthetic venue. That is enough to prove the pieces
compose. It is **not** evidence about the market.

**No forex card has ever been sent to Telegram.** The publisher's dispatch, its
idempotent claim and its double-press no-op are proven against fakes and against real
Postgres for the crypto path; the forex path has the fakes only.

**A forex signal HAS now been written to Postgres — this gap is closed** (owner
requirement K1, closed the same day). `tests/storage/test_forex_signal_persistence.py`
writes a real `ForexPlan` through the real `SignalRepository` into a real `signals` row
and reads it back through `plan_of`. Run against the local `sentinel_test` database at
revision `0011_forex_spine`: **8 passed**, and the full opt-in suite is **2132 passed, 1
skipped** with the dev database untouched.

It asserts, in order of what would hurt most: the row is stamped `market='forex'`; it
rehydrates as a **`ForexPlan`** and not as anything else; **every Decimal survives with
its scale intact** — compared as strings, because `==` is exactly the comparison that
could not see the capital defect; a crypto plan written beside it comes back a
`TradePlan`; a market-scoped read never returns the other market's row (previously only
ever tested against a table containing crypto rows, which is the condition under which a
missing filter also passes); and a forex record handed to a crypto-scoped repository is
refused before it reaches the table.

That is HANDOFF §4 item 12 applied to forex **before** it ships rather than after.

**The invented prices are still invented.** The correlations, the USD index and the
ladder in these tests are facts about a generator, not about EURUSD.

**Every threshold added in step 3 is an uncalibrated guess**, in the same standing as
`spread_max_multiple: 3.0`. So is `max_entry_distance_pct: 0.5` — argued from EURUSD's
daily range, not derived.

**The swap table is still empty and commission is still zero.** Any net-RR figure prices
the spread and nothing else, and the card now says `rollover not priced` rather than
`€0.00` — but a position held overnight will cost more than the card claims until the
owner fills the table.

**€200 still cannot trade this market.** The demo prints it:
`--capital 200` on a 35-pip stop returns `BELOW_MIN_TICKET`. §16.3 adds one level to
journal/M10b_REPORT.md §6's measurement — at that capital a 40% rung is a few hundred
units against a 1000-unit minimum, so **every ladder collapses to a single rung before
the whole ticket is refused**. The rung count is itself a reading on account size.

### The joins this milestone's own boundary leaves untested

The rule from M10b-2 §2, applied to myself. Switch-on day should compose these **first**:

1. ~~A forex signal has never been persisted by the real `SignalRepository`.~~
   **Closed** — see above.
2. **A forex tracker tick has never priced a real Saxo 1m tail.** `PriceFeed` over
   `SaxoForexAdapter.ohlcv` satisfies the protocol and has never been run.
3. **`/positions`, `/journal` and `/stats` have never rendered a forex row.** They are
   market-blind by construction and by type, and no row has gone through them.
4. **The forex analyst prompt has still never been sent to the model** — carried forward
   unchanged from M10b-2. No NO_SETUP rate, no token count, no cost figure.

---

## 14. Deploying this

**Not deployed.** When you are ready, `docs/DEPLOY.md` §13b is unchanged and is still the
procedure — the server's `config.yaml` predates `markets:` entirely, so forex cannot be
enabled by flipping a flag there, and crypto's reserved floor has to go in with the block
rather than after it.

First boot applies **no migration**. The app loads a config whose
`markets.forex.enabled` is still `false`, registers one `scan:crypto` job and one
`tracker` job, and runs a cycle behaviourally identical to the one before it — except
that the signal card and `/status` now print `€200.00` and `0.75%` where they printed
eighteen decimal places.

That last clause is the **only** user-visible change in this milestone, and it is the one
that was approved line by line before it was made.
