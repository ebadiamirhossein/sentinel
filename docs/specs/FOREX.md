# FOREX.md — the forex market (M10b, M10c)

Version 3, revised 2026-08-21. **§16 is new and was written at M10c, from the code** —
the forex plan, the forex gate, the card, publishing and tracking. Version 2 revised v1
(2026-08-20) from the spike findings.

v1 was written from vendor documentation and one SIM API call. The spike tested it
against the live API and found **eleven defects**, one of which would have made every
spread and stop wrong by a factor of ten while producing plausible numbers and no
error. Every correction is dated in the log at the end.

M10a taught Sentinel to hold more than one market. M10b adds the second one.

---

## §0 — Scope and the one hard rule

**In scope:** EURUSD, GBPUSD, USDJPY from Saxo Bank OpenAPI, delivered as
manual-execution trade plans through the existing Telegram bot, sized in EUR, measured
in its own DRY_RUN population.

**Out of scope:** metals, indices, additional pairs, any form of execution, and any
change to crypto.

**The hard rule from M10a:** crypto's behaviour does not change. `sentinel/risk/` stays
frozen — forex sizing is a **new module**, never an edit to the existing one. M10a's
golden cycle and golden surface tests are the gate and must stay byte-identical
throughout. If a forex change moves a crypto golden, the change is wrong.

Forex ships with `markets.forex.enabled: false` and turns on later by config edit,
starting in `dry_run: true` while crypto stays live.

---

## §1 — What is now measured

Everything below was tested against the **live** Saxo API on 2026-08-21. Raw evidence
is in `spike/raw/`. Nothing in this section is an assumption any more.

### Verified

| Fact | Evidence |
|---|---|
| OAuth authorization-code flow works on LIVE | 201, tokens issued, state verified |
| Live chart data works on an **unfunded** account | HTTP 200, no `NoAccess` |
| No paid market-data subscription needed for forex | Same |
| **The live spread is real and varies** | 46–95 distinct values per pair over ~50 days |
| The chart spread equals the dealable spread | infoprices firm quote 1.2 pips vs chart median 1.1; `PriceSourceType: Firm`, `DelayedByMinutes: 0` |
| `MinimumTradeSize` = **1000.0** on all three pairs | Reference data |
| Bars finalise within ~4 s of close and are stable after | 1-minute sampling across four rolls |
| Closed-market hours are **absent**, not repeated or zero-filled | Weekend probe |
| Every tail we need fits in **one** request | 321 max vs a 1200 ceiling |
| No volume field of any kind | Absent from every response |
| No candle-closed flag | Absent |

### Falsified

| v1 claim | Truth |
|---|---|
| An economic calendar is reachable through the API | **False.** Feature flag says `Calendar: true`, but no reachable path — 5/5 candidates 404 |

The 404s disprove those specific URLs, not the existence of some undocumented endpoint.
Practically it makes no difference: we cannot use what we cannot reach.

### Still a standing risk, not a settled fact

An unfunded live account has API access **today**. That cannot be proven for the
future. §12 covers losing it.

---

## §2 — The honest ledger

| Lost vs crypto | Replacement |
|---|---|
| Funding rate | Swap/rollover from config, 21:00 UTC, **triple Wednesday** |
| Open interest | **Nothing** |
| Long/short ratio | **Nothing** — Saxo's order/position book endpoints were discontinued |
| Volume, even tick counts | **Nothing** |
| Exchange order-book depth | **Measured spread series** (§7.3) — this one is genuinely good |
| 24/7 trading | Session structure (§5) |

Gained, all as deterministic features we compute, never prompt hints: prior-day and
prior-week high/low, daily and weekly open, session labels, a synthetic USD strength
index (deliberately not ICE-licensed DXY), rolling cross-pair correlation, and the
measured spread.

### §2.1 — Missing data must look missing

Where an input does not exist the feature is **absent** — not zero, not a placeholder,
and not quietly dropped from the prompt. The forex analyst prompt states plainly which
inputs are unavailable for this market.

A zero meaning "no data" reads to a model as "no activity" and yields a confident answer
built on nothing. **Test:** assert the assembled forex prompt contains the explicit
unavailability statement, and that no forex feature dict holds a zero standing in for a
null.

---

## §3 — Authentication *(corrected: D-b, D-c)*

**v1 implied "log in once, forever." That is false, and the correction is operational,
not cosmetic.**

Measured:

| | Value |
|---|---|
| Access token lifetime | ~1070–1200 s (**~20 min**), and it **varies between responses** |
| Refresh token lifetime | ~3582–3600 s (**~1 hour**) |
| Refresh token rotates on use | **Yes — single use** |
| Access token rotates on refresh | Yes |
| Token exchange status code | **201**, not 200 |
| Unattended credential available | **No.** Certificate Based Authentication is the only unattended option and is partners-only |

### Requirements

1. **Persist on every refresh.** The refresh token is single-use. A persist-once design
   works through the first refresh and then dies at whatever hour the container happens
   to restart — healthy-looking until it isn't.
2. **Verify the write by reading it back.** A 2xx from the token endpoint is not proof
   the token was saved. Assert the round-trip before reporting success. *(This exact
   bug occurred during the spike: a successful exchange printed success and persisted
   nothing.)*
3. **Accept any 2xx**, never one literal status code. Saxo returns 201 here.
4. **Read every lifetime from the response** and store an absolute expiry. The numbers
   drift between responses; a hardcoded constant is wrong by construction.
5. **Refresh every ~5 minutes**, well inside the 20-minute access window.
6. **Store in Postgres**, surviving restart and redeploy.
7. **On refresh failure: pause forex only**, alert the owner, leave crypto untouched.
   There is a test that forces a refresh failure and asserts a crypto cycle completes.
8. **Credentials never appear** in logs, process listings, or error messages — the
   standard `ops/lib.sh` already enforces.

### §3.1 — The one-hour memory is a **rare-event** cost, not constant toil *(reframed 2026-08-21, M10d)*

**This section previously read as ongoing work. It is not, and the difference decides
whether Saxo is a viable provider.** The refresh chain remembers for one hour and
**every refresh resets that hour**, so with the five-minute cadence running the window
is only ever consumed by an outage.

Measured against how this deployment actually behaves:

| event | duration | survives? |
|---|---|---|
| `docker compose up -d --build` | **~30 s** | yes, easily |
| the owner's last server reboot | **~90 s** | yes, easily |
| routine operation | — | yes; each refresh resets the hour |
| an outage **longer than an hour** | > 3600 s | **no — one manual browser login** |

So the honest statement is: **one manual login after an outage longer than an hour.**
Not "log in once, forever" (v1, false), and not constant toil (this section until
M10d, misleading). It is a rare-event cost, and it is the price of this provider at
this account tier — Certificate Based Authentication is the only unattended option
Saxo documents and it is partners-only.

Three things follow, and **all three were unimplemented until M10d** even though this
section had specified them since the spike (journal/M10d_REPORT.md P3):

1. **Something must call `bootstrap()`.** Otherwise `SAXO_REFRESH_TOKEN` never leaves
   `.env`, the store is empty at boot, and forex is dead on its first cycle with a
   valid credential sitting in the environment. `sentinel/core/app.py` seeds at boot;
   the stored credential always wins, so a redeploy cannot overwrite a live chain with
   the spent `.env` one.
2. **Something must call `ensure_fresh()` on a timer.** The `forex-token-refresh` job
   runs every `token_refresh_interval_seconds` (300) with jitter, registered only when
   forex is enabled, and it runs **through the weekend** — the market being shut does
   not pause the credential's clock.
3. **Something must catch `ReauthenticationRequired` and send the alert.** It names
   re-authentication as the action and carries the authorize URL, because a generic
   "forex paused" sends the owner hunting a data problem when the fix is a two-minute
   login. It is keyed on the **dead credential's fingerprint**, not on the date: one
   message per dead chain, and a fresh one if a fresh chain dies the same day.

The flow itself stays as designed: the owner logs in on his own Mac, the code lands on
`https://localhost:8080/callback`, and the refresh token goes into the server's `.env`.
This preserves the deployment's **zero inbound ports** property.

---

## §4 — The adapter

New module `sentinel/ingestion/forex_saxo.py` implementing the existing
`MarketDataAdapter` protocol, registered in `core/wiring.py` under `forex_saxo` — the
key M10a deliberately left unregistered.

### §4.1 — Candle completeness *(confirmed sufficient)*

Saxo sends **no closed flag**. Feeding a forming candle into RSI, ATR or the EMA stack
makes every indicator wrong on the newest bar — plausibly wrong, with no error.

**Rule:** a candle stamped `T` with horizon `H` is closed iff `now >= T + H + grace`.

**Measured grace: publish lag 0–4 s across four observed rolls. Use 30 s** — 4 s
observed max, 5 s poll pessimism, ~3× headroom, negligible against a 60-minute bar.

Bars finalise within ~4 s and are stable thereafter, so the clock rule is sufficient;
no re-fetch-and-compare is needed. Test the boundary with a frozen clock: one tick
before, exactly at, and one tick after.

### §4.2 — Instrument resolution *(corrected: D-a, D-h)*

Uics are resolved at startup from `/ref/v1/instruments` by keyword and cached in
`instrument_meta`. **No Uic is hardcoded.**

**The pip rule, which v1 got wrong:**

```
pip = 10 ** -Format.Decimals
```

And it is **asserted** against `TickSize × 10`, which must agree.

| Pair | Uic | `Format.Decimals` | pip | `TickSize` | `TickSize × 10` |
|---|---|---|---|---|---|
| EURUSD | 21 | **4** | 0.0001 | 1e-05 | 0.0001 ✓ |
| GBPUSD | 31 | **4** | 0.0001 | 1e-05 | 0.0001 ✓ |
| USDJPY | 42 | **2** | 0.01 | 0.001 | 0.01 ✓ |

`Decimals` is the **pip** precision. `AllowDecimalPips` means the venue displays one
further fractional-pip digit, which is why `TickSize` is a tenth of a pip everywhere.

**Why this defect is dangerous.** v1 said "5 decimals, pip 0.0001" — values correct,
reasoning wrong. Read naturally that yields `10**-(decimals-1)`, which against the real
`Decimals=4` gives **0.001: every spread and stop wrong by 10×**, still plausible, no
error anywhere. This exact mistake was made during the spike and caught only because
the API contradicted the spec. The `TickSize × 10` cross-check is therefore a **required
assertion in code**, not a note in a document.

Chart v3 returns **null** `ChartInfo` and `DisplayAndFormat` (corrected 2026-08-21 from
the spike's "empty objects"), so precision comes from reference data **only** — there is
nothing in the chart payload to read even by accident.

Unresolvable instruments are skipped with a named reason, never silently.

### §4.3 — Symbol namespace

`EURUSD`, `GBPUSD`, `USDJPY`.

`ohlcv_candles` keeps its `(symbol, timeframe, open_time)` primary key while carrying a
`market` column, so identical symbol strings across markets would collide. They are
disjoint today. **Startup assertion:** no enabled forex symbol equals any enabled crypto
symbol — so an overlap is found loudly rather than through corrupted candles.

### §4.4 — Timeframes, tails and paging *(corrected: D-d, D-e, D-j, D-k)*

**Tail lengths are 321/321/321/101**, not v1's 201/201/201/101. The running `config.yaml`
requests 321 for 15m/1h/4h so that 320 closed candles give 121 valid EMA200 points
across the 120-candle chart window.

**Paging is forbidden.** Measured: every tail we need fits in a single request.

| TF | Horizon | Asked | Returned |
|---|---|---|---|
| 15m | 15 | 321 | 321 |
| 1h | 60 | 321 | 321 |
| 4h | 240 | 321 | 321 |
| 1d | 1440 | 101 | 101 |
| 1m (tracker) | 1 | 5 | 5 |

Largest is 321 against a 1200 ceiling — 27% of one request.

Forbidding paging is not just simpler, it is **safer**, because of a reproducible
anomaly the spike found: **the same hour can be present or absent depending on the
request's anchor and `Count`.** Sunday 19:00Z/20:00Z appeared at `From Fri 18:00Z,
Count ≥ 50` and was absent in six other framings. Deterministic, reproducible,
mechanism unknown — and deliberately not guessed at. A page boundary is exactly where
that instability would produce a tail with a hole or a duplicate.

Two guards regardless:
- **Assert `len(returned) == requested`.** Over-requesting `Count` silently clamps to
  1200 rather than erroring.
- A short return is a **degraded read**, not a shorter tail.

**Bar alignment differs by timeframe.** 1m, 15m and 1h sit on the UTC grid. **4h and 1d
are anchored to 17:00 America/New_York and shift with US daylight saving** — 4h runs
01/05/09/13/17/21:00Z in August and 02/06/10/14/18/22:00Z in January. The 1d `Time`
stamp is a date label, not the bar's start.

Consequence: **a forex 4h bar never aligns with a crypto 4h bar.** They are separate
markets with separate populations, so nothing breaks — but no code may assume a shared
grid, and no chart may imply one.

---

## §5 — Market hours, sessions, the weekend

### §5.1 — Closed is not degraded

Crypto's staleness rule — OHLCV older than 2× the timeframe means DEGRADED — would fire
on every weekend snapshot and fill the logs with false failures.

**Market hours are checked before staleness.** Closed is a normal state with its own
reason code. Measured and confirmed: closed hours are cleanly **absent** from the
response, not repeated and not zero-filled, which is the outcome that makes this
straightforward. Tested.

### §5.2 — The trading week *(amended: D-d)*

Roughly Sunday 21:00 UTC to Friday 21:00 UTC, and **the boundary moves by an hour twice
a year** because the US and EU change DST on different dates. Derive it from actual
candle availability, with nominal times in config as a sanity check; log any divergence.

**Open ambiguity:** the Sunday week-open is either 19:00Z or 21:00Z depending on the
query framing (D-d). Until that is resolved, treat the earliest reliably-present Sunday
hour as the open and **do not emit signals in the first hours of the week.** Nothing is
lost — Sunday-evening liquidity is thin anyway.

### §5.3 — Rollover: make it spread-triggered, not time-boxed *(corrected: D-g)*

**v1's ±15 minutes was far too narrow.** Measured: elevated spreads run **19:00–21:00
UTC** — three hours. At 21:00, GBPUSD's median spread is **12.0 pips, which is 0.667R on
an 18-pip stop.**

Since we now measure the spread, the better rule is **spread-triggered rather than
clock-triggered**: no new signal when the current spread exceeds a configured multiple of
that instrument's median. Self-calibrating, and it also catches unscheduled widening that
no clock would.

**Corrected 2026-08-21 (defect #15).** This section originally said "median **for that
hour-of-day**", and that cannot work. At 21:00 UTC GBPUSD's hour-of-day median *is* 12.0
pips, so a 12-pip spread at 21:00 is "normal for that hour" and passes the gate — while
costing 0.667R of an 18-pip stop. The per-hour baseline normalises away precisely the
widening the gate exists to catch, and the gate never fires at the one hour it was
written for. So:

- the **cost model** uses the **per-hour-of-day** median — the right expectation of what
  trading in this hour costs, and what feeds net RR;
- the **gate** uses the instrument's **global median** across the whole sample, so
  rollover hours fail it naturally and a genuine anomaly fails it at any hour.

The multiple is **3.0**, against the global median, and it is a starting guess to be
calibrated from DRY_RUN data rather than a derived figure. Sanity check: EURUSD's global
median of 1.1 gives a 3.3-pip threshold, GBPUSD's 1.8 gives 5.4, and its 21:00 median of
12.0 fails that. Every firing is logged with instrument, hour, spread and threshold,
because how often it fires is itself a measurement.

Keep a time-based floor of 19:00–21:00 UTC as a backstop for when the spread series is
unavailable. In bar stamps that is 19, 20 and 21 — the three bars the spike measured as
elevated — which is wall-clock 19:00–22:00.

**Corrected 2026-08-21 (defect #30): the design above does not currently cover the window
it was written for, and the backstop cannot fire.**

The two halves were meant to compose — measured rail primary, clock floor for when the
measurement is missing. In production the measurement is **never** missing: the 1h tail
is 1200 bars every cycle, far past `spread_min_samples`, so `spread_gate` never reaches
its clock branch and `ROLLOVER_WINDOW` is **unreachable**. The measured rail therefore
decides the rollover hours alone, and against the global median at 3.0× it admits them:

| | EURUSD | GBPUSD | USDJPY |
|---|---|---|---|
| global median | 1.1 | 1.8 | 1.5 |
| threshold at 3.0× | **3.3** | **5.4** | **4.5** |
| hour-of-day median 19:00 | 1.1 → passes | 1.8 → passes | 1.6 → passes |
| hour-of-day median 20:00 | 1.5 → passes | **4.6 → passes** | 1.9 → passes |
| hour-of-day median 21:00 | 2.7 → passes | 12.0 → rejects | 4.2 → passes |

Only GBPUSD at 21:00 fails. Every other rollover-hour bar at its own typical spread is
admitted — so a rail written specifically to stop trading through the widening D-g
measured lets almost all of it through, while the cost model charges that same widening
at row 9 and rejects most of the resulting plans on net RR. **The cycle is paid for in
full to produce a plan the next rail throws away.**

This is defect #21's own sin one level out: *a rail that cannot fire is worse than an
absent one, because it reads on a checklist as a rail.*

**Deferred, deliberately, and the reason is worth stating.** The fix is a lower multiple,
or a second per-hour trigger, or an absolute pip cap — every one of them a **new
uncalibrated guess**, which is exactly what DRY_RUN calibration exists to avoid setting
blind. So M10d sidesteps the window instead: `forex.scan_hours_utc` ends at **19**, which
is already `friday_signal_cutoff_hour_utc` and therefore invents no number. The exposure
is closed for the observation window and the defect is not. `tests/fx/test_spread.py`
pins the unreachability so a future fix has something to invert, and pins that the scan
window ends at or before `rollover_window_start_hour_utc` so the exposure cannot be
re-acquired by widening the window alone.

### §5.4 — The weekend gap

A pending ladder cannot fill over a weekend. A filled position gaps through its stop on
Monday. Neither has any analogue in a 24/7 market.

- No new signals after a configured Friday cutoff.
- **All pending entry ladders expire before the Friday close** — expired, with a reason
  the owner sees, not paused.
- Positions open into the weekend carry a clear gap warning on the card.
- Cycles skipped while closed; this also saves LLM spend.
- The tracker idles rather than erroring, and never reads a frozen price as a stall.

---

## §6 — Features and charts

Shared feature engine. Forex adds computed features and omits impossible ones.

**Added:** prior-day and prior-week high/low, daily and weekly open, session labels,
synthetic USD strength index, rolling cross-pair correlation, measured spread series.

**Omitted:** every volume-derived feature — omitted, not zeroed (§2.1).

**Charts:** same renderer, forex variant. No volume panel at all rather than an empty
one. Prior-day and prior-week levels drawn. Session shading. No funding annotation.

### §6.1 — Daily alignment: RECOMMENDATION REVERSED *(corrected: D-k)*

v1 recommended UTC alignment for determinism. **The spike changes that recommendation.**

Saxo's native 4h and 1d bars **are** the 17:00 New York convention, DST and all. So UTC
alignment is no longer free — it would mean aggregating daily and 4h bars ourselves from
hourly candles.

**Now recommended: use Saxo's native 17:00 NY alignment.** Three reasons:

1. **The levels must match what the owner sees.** He executes manually in SaxoTraderGO.
   If our "yesterday's high" differs from the platform's, the plan is unusable at exactly
   the moment it matters.
2. **Aggregating ourselves adds a new class of error** — DST handling, gap handling,
   partial-day handling — to save nothing.
3. It is the FX market convention, which is what the other participants are trading off.

The cost is that forex daily boundaries differ from crypto's, and that 4h bars shift by
an hour twice a year. Both are fine: separate markets, separate populations, and the
alignment is read from the data rather than assumed.

*Owner's call — this reverses my earlier recommendation on the strength of D-k.*

---

## §7 — Sizing and cost

A **new module**, built test-first from hand-computed fixtures exactly as M4 was.
`sentinel/risk/` is not edited; its 100% branch coverage stays unchanged.

### §7.1 — The sizing formula

```
units        = risk_eur × rate(EUR→quote_ccy) / stop_distance_in_quote
notional_eur = units × price_quote / rate(EUR→quote_ccy)
```

`rate(EUR→quote_ccy)` is how many quote-currency units one euro buys.

| Instrument | Quote | rate(EUR→quote) | Source |
|---|---|---|---|
| EURUSD | USD | EURUSD (~1.169) | Frankfurter, already in the system |
| GBPUSD | USD | EURUSD (~1.169) | Frankfurter |
| USDJPY | JPY | **EURJPY** (~170) | Frankfurter |

USDJPY needs **EURJPY**, not EURUSD. Frankfurter already serves EUR-based rates for USD,
GBP and JPY — no new dependency.

Pip values come from §4.2's rule, asserted against `TickSize × 10`. Never from
convention.

### §7.2 — The minimum-ticket problem

`MinimumTradeSize = 1000.0` confirmed on all three pairs. At EURUSD 1.169, €200 capital,
0.75% risk (€1.50):

| Stop | Ideal units | vs the 1,000 minimum |
|---|---|---|
| 18 pips | **974** | just below — would be 1.027× intended risk |
| 25 pips | **701** | well below |
| 40 pips | **438** | far below |

**A wider stop needs a smaller position**, so wider stops make this worse.

Inverted: minimum viable capital is about **€205 at an 18-pip stop** and about **€456 at
40 pips**.

Two consequences, both to be stated in the M10b report rather than papered over:

1. At €200, only tight-stop setups size correctly — and tight stops are where spread
   hurts most (§7.4).
2. The engine returns an explicit **`BELOW_MIN_TICKET`** rejection. How often it fires is
   itself a measurement worth having.

**Never round up into more risk than the budget allows.** Under-risking is fine;
over-risking is not.

### §7.3 — Costs *(upgraded from assumption to measurement)*

**The spread is measured, not configured.** This was v1's biggest open question and it
resolved in our favour.

| Pair | Min | **Median** | Max | Distinct values |
|---|---|---|---|---|
| EURUSD | 1.0 | **1.1** | 18.3 | 46 |
| GBPUSD | 1.1 | **1.8** | 41.3 | 95 |
| USDJPY | 1.2 | **1.5** | 31.4 | 65 |

*(pips, ~50 days of hourly bars)*

Confirmed against a firm dealable infoprices quote at 1.2 pips vs a 1.1 chart median,
`DelayedByMinutes: 0`. The four OHLC fields carry **different** spread sets — structurally
the opposite of SIM's constant offset.

So: store the spread series per instrument per hour-of-day, and feed the **measured**
value into the net-RR gate.

Plus **commission** from config, and **swap/rollover** from config per instrument per
direction, charged 21:00 UTC and **tripled on Wednesday**, included for the expected
holding period.

### §7.4 — What this does to the gate *(numbers corrected)*

v1 estimated 0.11R from SIM's synthetic 2.0 pips. **Measured EURUSD cost on an 18-pip
stop is 0.061R** — v1 was pessimistic by about half.

**Corrected 2026-08-21 (defect #16).** The arithmetic below originally computed net RR
as `gross − cost/risk`. That is not what a round-trip spread does. §7.5 settles that
levels come from the **bid** series and that execution is asymmetric — a long enters at
ask and exits at bid — so one spread `s` makes the loss `risk + s` **and** the gain
`reward − s`. It lands on both sides at once:

```
net   = (gross × risk − s) / (risk + s)
gross = target + (1 + target) × s / risk        # inverted for the uplift needed
```

which is also the shape `sentinel/risk/costs.py` has used for crypto since M4 — costs
that fall on the losing side go in the denominator, costs paid on the way out of a
winner come off the numerator. Forex was the odd one out, not the crypto engine.

Restated on an 18-pip stop, from a gross 1.5. The struck figures are what this section
printed before the correction:

| Pair | Median spread | Net from a gross 1.5 | Gross RR needed to net 1.5 |
|---|---|---|---|
| EURUSD | 1.1 | **1.356** (was 1.439) | **1.653** (was 1.561) |
| USDJPY | 1.5 | **1.308** (was 1.417) | **1.708** (was 1.583) |
| GBPUSD | 1.8 | **1.273** (was 1.400) | **1.750** (was 1.600) |
| **GBPUSD at 21:00** | **12.0** | **0.500** (was 0.833) | **3.167** (was 2.167) |

The qualitative conclusion survives: **any positive cost sinks a gross 1.5**, so the gate
will reject setups that look acceptable before costs. What changes is the size of it —
the required uplift is about **two and a half times** what this section claimed, because
the cost is charged `1 + target` times rather than once. It is still modest in normal
hours and brutal at rollover, which is precisely what §5.3's spread trigger exists to
catch.

### §7.5 — Which side of the spread *(new — neither v1 nor the spike covered this)*

An unresolved gap that must be decided before the sizing module is built.

Every candle carries both bid and ask. **Which do features use, and where does the
spread enter the plan?**

**Proposed:** compute all features and levels from **bid** candles for consistency, then
model execution asymmetrically — a long enters at ask and exits at bid, a short the
reverse. The round-trip cost is one full spread, which is what §7.4 assumes.

This must be explicit and tested, because getting it wrong shifts every level by the
spread — one pip on EURUSD, but twelve at rollover on GBPUSD.

### §7.6 — Margin *(corrected: D-f)*

CFD margin is account-level. There is no per-position liquidation price, so the crypto
liquidation-buffer rule does not apply and must not be faked.

v1 said "read the venue's actual leverage figure." **That is not satisfiable** — the
instrument details carry no `MarginRates` or `MarginTiers`. So:

- Maximum leverage comes from **config**, defaulting to the ESMA retail cap of 30:1 for
  majors, and is **labelled a documented assumption** wherever it surfaces.
- required margin = notional / configured max leverage
- a rail on total forex margin as a share of equity
- card text stating plainly that stop-out risk is account-level, not per-position

---

## §8 — Economic calendar *(corrected: the assumption was false)*

**There is no reachable Saxo calendar.** The feature flag says `Calendar: true`; every
probed path 404s.

So the hand-maintained YAML is **not a backstop — it is the only source.** That is real
recurring manual work and the spec should not pretend otherwise.

**Requirements:**
- A YAML of high-impact events: central bank decisions, CPI, NFP, GDP, PMI, tagged by
  currency and impact.
- A **staleness rail**: if the YAML's coverage ends within N days, alert the owner. An
  expired calendar must never silently become "no events today."
- Central bank meeting dates are published a year ahead, so the recurring burden is
  mostly the monthly data releases.

**Policy — open decisions, recommendations unchanged from v1:**
- Suppress new signals from −60 to +30 minutes around high-impact events, currency-matched.
- **Cancel pending entry ladders** for the affected currency ahead of high-impact events.
- Annotate open positions with a warning rather than closing them. Sentinel informs, never
  instructs.

---

## §9 — Portfolio rails: correlation

EURUSD, GBPUSD and USDJPY all cross the dollar. Long EURUSD plus short USDJPY is one
large short-dollar bet wearing two hats, and the existing rails would allow both.

**v1 recommendation stands: a hard cap of one open forex position at a time** for the
first window. Crude, safe, and it makes the first measurement interpretable. Relax to a
correlation-adjusted rail once there is data to calibrate against.

Forex rails are entirely separate from crypto's — M10a's per-market rails already support
this.

---

## §10 — News *(amended)*

CryptoPanic is crypto-only and unused here.

**Primary: central bank RSS** — Fed, ECB, BoE, BoJ. Official, free, high signal.

Saxo's news service reported available but **3/4 probed paths 404**, and
`/trade/v1/messages` is broker messaging returning `[]`. **Do not plan around it.**

The `untrusted_news_data` delimiter defence from M5 applies unchanged. External text is
data, never instruction.

---

## §11 — Telegram, stats, populations

M10a did this work; M10b consumes it.

- Forex cards are market-tagged via M10a's `multi_market` condition. **When forex is
  switched on, crypto cards gain their tag too — which changes crypto's card bytes.** The
  golden surfaces must therefore be **regenerated deliberately at switch-on**, as a named
  step, never quietly during the build. Note it explicitly in the M10b report.
- `/stats`, `/pulse`, `/positions`, `/watchlist`, `/journal` already section per market and
  never merge figures.
- Populations are REAL / HYPOTHETICAL / DRY_RUN × market. `summarize()` already raises on
  rows spanning more than one market.
- **Forex starts in DRY_RUN** and stays there until the numbers justify otherwise.

---

## §12 — Failure modes *(expanded: D-i)*

Every one degrades **forex only**. Each needs a test asserting a crypto cycle completes
normally while forex is in that state.

| Failure | Behaviour |
|---|---|
| Refresh token expired (outage > 1 h) | Pause forex; alert **naming re-authentication** and carrying the authorize URL |
| OAuth refresh fails | Pause forex, alert, crypto untouched |
| Chart endpoint errors or times out | Skip symbol with a named reason; repeated failures pause forex |
| Returned candle count < requested | **Degraded read**, not a shorter tail |
| Instrument resolution fails | Skip symbol, named reason, never silent |
| `MarketDataViaOpenApiTermsAccepted` flips to false | Named potential pause cause — it currently reads **False while data flows**, so it is not a reliable gate, but a change in it is worth alerting on |
| Calendar YAML stale or missing | **Suppress signals** rather than trade blind |
| Frankfurter unavailable | Existing last-known-good; if too stale, no sizing, so no signal |
| Market closed | Normal state, cycle skipped, no error |
| Forex sub-budget exhausted | Forex pauses, crypto continues (M10a, tested) |

---

## §13 — Open decisions for the owner

1. **Daily/4h alignment** (§6.1) — **recommendation reversed to Saxo's native 17:00 NY.**
   The alternative now costs self-aggregation and desynchronises our levels from the
   platform the owner executes on.
2. **Blackout cancels pending ladders?** (§8) *Recommend yes for high-impact events.*
3. **Blackout window** (§8) — *Recommend −60 / +30 minutes, currency-matched.*
4. **Max concurrent forex positions** (§9) — *Recommend 1 for the first window.*
5. **Forex capital** (§7.2) — €200 sizes only tight-stop setups. *Recommend keeping €200:
   forex starts in DRY_RUN, so sizing is measured with nothing at stake, and the
   `BELOW_MIN_TICKET` rate tells us what capital would actually be needed.*
6. **Crypto budget floor** (M10a R5) — with crypto 10.00 + forex 4.00 under an 11.00
   ceiling, a forex-heavy morning can leave crypto short once forex is live. *Recommend a
   reserved floor of 8.00 for crypto, so a new unmeasured market can never squeeze the
   measured one.*
7. **Which side of the spread** (§7.5) — *Recommend bid-based features with asymmetric
   execution modelling.* New question; needs a decision before the sizing module.

---

## §14 — Build order

1. **Adapter** — candle completeness and instrument resolution tested first, with the
   `TickSize × 10` pip assertion.
2. **Market hours**, before anything consumes forex data, so staleness never misfires.
3. **Auth hardening** — persist-on-every-refresh with read-back, 5-minute cadence,
   re-authentication alert.
4. **FX sizing module**, test-first, hand-computed fixtures, `sentinel/risk/` untouched.
5. **Costs and net-RR**, with the spread-triggered rollover gate.
6. **Calendar YAML and blackouts**, including the staleness rail.
7. **Features and charts.**
8. **Wiring**, still behind `enabled: false`.

Crypto goldens run at every step. Nothing turns on until the owner says so.

---

## §15 — Exit criteria

- `markets.forex.enabled: false` in the shipped config; deploying M10b is a **no-op**.
- Crypto goldens byte-identical; `git diff sentinel/risk/` empty.
- `make check` green; migration verified against a fresh production dump.
- Pip derivation asserted against `TickSize × 10` for all three pairs, with a test proving
  a wrong derivation fails loudly.
- FX sizing has hand-verified fixtures for all three pairs, including USDJPY's EURJPY
  conversion and its 0.01 pip.
- Candle-completeness boundary tested with a frozen clock at 30 s grace.
- Auth: a test proving a persist failure is caught, and one proving crypto survives a
  forex auth failure.
- Every §12 failure mode has a test proving crypto survives it.
- `journal/M10b_REPORT.md` records the `BELOW_MIN_TICKET` rate at €200, the observed
  spread distribution in production, and every spec defect found during the build — dated.

---

## §16 — the forex plan and the forex gate *(written at M10c, from the code)*

M10b-1 stopped at a sizing result because defect #12 found that `TradePlan` could not
carry a forex plan. §7 and §11 assumed one could. This section is the half that was
missing, and it is written **from the code rather than ahead of it** — deliberately, and
for the reason defect #12 exists: the first attempt to specify this part was wrong in a
way only reading `sentinel/risk/models.py` revealed.

### §16.1 — Why not a `TradePlan`, and why not a subclass

`TradePlan` carries five fields forex has no meaning for — `notional_usdt`,
`suggested_leverage`, `liq_distance_pct`, `liq_buffer_ok`, `instrument: InstrumentMeta` —
and its `PlanCosts` carries six more under `funding_*`. Filling them for forex means
either a lie (`liq_buffer_ok=True` on a market with no per-position liquidation price) or
a zero standing in for a measurement. §7.6 forbids the first outright and §2.1 forbids the
second. `sentinel/risk/` may not be edited, so subclassing (which inherits the forbidden
fields) and a shared base class (which requires editing `TradePlan`) are both out.

**Ruling: a parallel, independently-defined model.** `ForexPlan` in
`sentinel/fx/plan.py`, holding the same rule `ForexSizing` and `ForexCosts` are already
held to — *if a concept does not exist in this market, there is no field to put it in* —
asserted by a test rather than left to habit.

### §16.2 — `ForexPlan`, in three groups

**Group 1 — mirrored from `TradePlan`, identical spelling and identical meaning.**
`schema_version`, `plan_id`, `created_at`, `symbol`, `direction`, `setup_type`,
`timeframe_label`, `confidence`, `report`, `entries`, `avg_entry`, `avg_fill_price`,
`stop`, `targets`, `rr_targets` (gross), `rr_targets_net`, `target_distances_pct`,
`costs`, `stop_distance_pct`, `last_price`, `planned_risk_eur`, `risk_eur`,
`notional_eur`, `margin_eur`, `management_plan`, `expires_at`, `capital_eur`,
`risk_per_trade_pct`, `gate_status`.

The mirroring is **load-bearing, not cosmetic**: `storage.repositories.signal_row()`,
`bot/publisher.py`, `tracker/loop.py` and every `/positions`, `/journal` and `/stats`
reader address a plan by these names. Matching them is what lets one signals table, one
publisher and one tracker serve both markets with no translation layer — and §16.7 is the
guard that keeps the resemblance from becoming a hazard.

**Group 2 — forex-only, because these are real here.** `quote_currency`,
`notional_quote`, `pip`, `pip_value_eur`, `stop_distance_pips`, `eur_quote_rate`,
`max_leverage`, `leverage_basis`, `margin_pct_of_equity`, `expiry_basis`,
`weekend_gap_warning`, `instrument: ForexInstrument`.

Three of those need their reason stated:

* **`eur_quote_rate`, not `eurusd_rate`.** §7.1: USDJPY sizes through **EURJPY**. A field
  named `eurusd_rate` holding an EURJPY figure is a fabricated label on a correct number,
  and the ~145× error it invites sizes a position to roughly nothing while looking like an
  unremarkable rejection.
* **`max_leverage` with `leverage_basis`, not `suggested_leverage`.** Crypto *derives* a
  leverage from the liquidation buffer. Forex has no per-position liquidation price
  (§7.6), so there is nothing to derive: 30:1 is a **configured cap** resting on the ESMA
  retail assumption, and the words travel with the number so no renderer can show one
  without the other. Two different concepts must not share a field name.
* **`expiry_basis`**, in words — `"12h intraday TTL"` or `"Friday close"` — because §5.4
  gives a ladder two possible reasons to die and a bare timestamp cannot say which.

**Group 3 — absent, and asserted absent.** No `notional_usdt`, no `suggested_leverage`,
no `liq_distance_pct`, no `liq_buffer_ok`, no `eurusd_rate`, no `InstrumentMeta`, and
nothing named `funding_*`. `costs` is a `ForexCosts`, whose swap is `rollover_*`.

### §16.3 — `ForexEntryRung` and the ladder

`ForexEntryRung` mirrors `EntryRung` with **`units`** where crypto has `qty`, keeps
`price`, `weight_pct`, `notional_eur` and the signed `distance_pct`, and has no
`notional_usdt`.

`sentinel/fx/ladder.py` follows §3's rules exactly — zone width below
`0.5 × ATR(1h)` gives one rung at the midpoint, otherwise three at 40/35/25 of the **risk
budget** — with one substitution: the collapse trigger is `MinimumTradeSize` (1000 units,
§7.2) instead of a minimum notional, and the collapse walks 3 → 2 → 1 exactly as
`risk/ladder.py` does.

**A consequence §7.2 implies but never states.** At €200 capital a 40% rung is roughly
400 units against a 1000-unit minimum, so **every forex ladder collapses to a single
rung** at this account size. That is not a bug and it is not worked around: it is the
`BELOW_MIN_TICKET` edge §7.2 asks to have measured, arriving one level earlier than
expected. The rung count is therefore itself a reading on whether €200 is a viable size.

### §16.4 — `ForexGateStatus` and `ForexGateDecision`

`ForexGateStatus` is a **new** StrEnum carrying the same three wire values as
`GateStatus` — `APPROVED_FOR_HUMAN`, `REJECTED`, `DOWNGRADED_WATCHLIST` — so
`gate_decisions.status` and every `/pulse` and `/stats` aggregation group both markets
identically without either enum importing the other. A test asserts the two vocabularies
still agree, so a divergence is a failure rather than a quietly split histogram.

It does **not** import `GateStatus`. `sentinel/fx/` depends on nothing in
`sentinel/risk/` — the precedent `fx/rounding.py` set, and for its stated reason: new-market
arithmetic must not be coupled to a module nobody is allowed to touch.

`ForexGateDecision` mirrors `GateDecision` with `reason: ForexRejection | None` and
`plan: ForexPlan | None`.

### §16.5 — The gate is a composition, in a fixed order

Every rail already exists in `sentinel/fx/` as a pure verdict function. `fx/gate.py` runs
them in the order `risk/engine.py` established — most specific first, net RR **last**, so
the harder blocker always wins and a rejection names the thing that actually stopped it:

| # | check | implementation | codes |
|---|---|---|---|
| 1 | preconditions | new | `NOT_A_CANDIDATE`, `MISSING_PLAN_FIELDS`, `NO_CAPITAL`, `FX_RATE_UNAVAILABLE`, `INSTRUMENT_UNRESOLVED`, `ATR_UNAVAILABLE` |
| 2 | the clock (§5) | `fx.hours.clock_verdict` | `MARKET_CLOSED`, `WEEK_OPEN_QUIET`, `FRIDAY_CUTOFF` |
| 3 | the calendar (§8) | `EconomicCalendar.blackout` | `CALENDAR_STALE`, `EVENT_BLACKOUT` |
| 4 | the spread (§5.3) | `fx.spread.spread_gate` | `SPREAD_TOO_WIDE`, `ROLLOVER_WINDOW` |
| 5 | geometry and quality | new `fx/coherence.py` | `ENTRY_ZONE_INVALID`, `STOP_SIDE`, `TARGET_ORDER`, `ENTRY_TOO_FAR`, `STOP_TOO_TIGHT`, `STOP_TOO_WIDE`, `RR_TOO_LOW`; `LOW_CONFIDENCE` **downgrades** rather than rejects |
| 6 | rails (§9) | new `fx/rails.py` | `PAUSED`, `MAX_CONCURRENT_POSITIONS`, `SYMBOL_COOLDOWN`, `DAILY_SIGNAL_CAP` |
| 7 | ladder and sizing (§7.1, §7.2) | `fx/ladder.py` + `fx.sizing.size_position` | `BELOW_MIN_TICKET` |
| 8 | margin (§7.6) | new | `MARGIN_ABOVE_EQUITY_SHARE` |
| 9 | costs and **net** RR (§7.3, §7.4) | `fx.costs.estimate_costs` + `net_rr` | `NET_RR_TOO_LOW` |

Rows 2, 3 and 4 sit **before** the expensive work on purpose: a shut market, a blackout
and a rollover spread are all cheap to establish and all of them make the rest moot.

Row 9's cost model uses the **per-hour-of-day** median (§5.3 as corrected by defect #15)
while row 4's gate used the **global** one. Two baselines answering two different
questions, and the gate would never fire at the one hour it was written for if they were
the same number.

### §16.6 — The new rejection codes

`ForexRejection` gains sixteen members for rows 1, 5, 6 and 8. Where a concept is
genuinely the same as crypto's the **name is the same**, so a `/pulse` rejection histogram
reads alike across markets; where it is not, the name says so. `MARGIN_ABOVE_EQUITY_SHARE`
is the one deliberate divergence from crypto's `MARGIN_BUDGET_EXCEEDED`: crypto budgets
margin per position, forex's margin is account-level, and one name over two meanings is
how §7.6's warning gets forgotten.

A meta-test asserts every member has user-facing wording, mirroring the guard that already
covers `SkipReason`.

### §16.7 — Where a forex signal lives, and the rule that keeps it distinguishable

**In the existing `signals` table, with no migration.** `market` is already a column,
`plan` and `chart_params` are already JSONB, and `signal_row()` reads only the attribute
names §16.2 group 1 mirrors. `SignalRecord.plan` widens to `TradePlan | ForexPlan`
(contract change, owner-approved 2026-08-21).

**The hazard this creates, and the rule.** §16.2's mirroring means the two models look
alike, and a mis-dispatched rehydration could produce a *plausible object* rather than an
error — the same shape as the pip derivation, where the wrong reading gives believable
numbers and no exception. So:

1. Rehydration dispatches on **`row.market`**, never on trying one model and falling back
   to the other. A fallback is precisely the mechanism that turns a mis-dispatch into a
   plausible object.
2. A test asserts both directions **fail loudly**: a forex row cannot validate as a
   `TradePlan`, and a crypto row cannot validate as a `ForexPlan`.
3. A structural test asserts **neither model's field set is a subset of the other's**, so
   a future field addition cannot quietly make one plan validate as the other. That is the
   property (2) actually rests on, and pinning the property rather than the two examples is
   what keeps it true after the next edit.

### §16.8 — The card

`forex_signal_card` lives in a **new module**, `sentinel/bot/forex_cards.py`, not as a
branch inside `cards.py`. Two reasons: the crypto renderer's diff stays empty, and the new
module joins `RENDERING_MODULES` so the AST no-arithmetic scan and the
every-number-came-from-the-plan check cover it from its first line rather than from
whenever somebody remembers.

Beyond §1's shape it carries what this market has and crypto's card has no room for:
distances in **pips** beside percentages, the measured spread with its basis, the rollover
nights, the ESMA words with the leverage, the §7.6 sentence stating plainly that stop-out
risk is account-level rather than per-position, and §5.4's weekend gap warning. It carries
the market tag under §11's `multi_market` condition, exactly as the crypto card does.

### §16.9 — Publishing

Unchanged mechanism. `SignalPublisher` already claims on `plan_id` and on
`(signal_id, kind, chat_id)`, and neither key knows or cares what shape the plan is. The
publisher picks the renderer by `record.market`. The double-press no-op is **proven for
forex by its own test**, not inherited by argument.

### §16.10 — The tracker in a market that is shut 49 hours a week

Four changes, and the first is the one with no crypto analogue at all:

1. **Closed is not a stall.** The loop skips detection while `fx.hours.state_at` reports
   CLOSED. A frozen price over a weekend is not a signal about anything, and reading it as
   one would resolve outcomes against a market that was not trading.
2. **Closed hours do not count against a TTL.** A twelve-hour ladder placed on Friday
   afternoon has not had twelve hours to fill by Sunday evening.
3. **Every pending ladder expires before the Friday close** (§5.4), at
   `friday_ladder_expiry_hour_utc`, with `expiry_basis` saying so. Expired with a reason
   the owner sees — never paused.
4. **A currency-matched high-impact event cancels pending ladders** (§8), through
   `EconomicCalendar.affected_symbols`. Open positions are annotated, never closed:
   Sentinel informs and does not instruct.

`prices.PriceFeed`'s candle ceiling becomes the adapter's own rather than Binance's
hard-coded 1000 — Saxo's is 1200, and it is already `ForexConfig.max_count`.

### §16.11 — What this section does not settle

Every threshold it introduces is a **starting guess to be calibrated from DRY_RUN**, in
the same standing as `spread_max_multiple: 3.0`. The swap table is still empty and
commission is still a configured zero, so any net-RR figure prices the spread and nothing
else. And no live forex cycle has ever run: everything here is exercised against a
synthetic venue.

---

## Corrections log

**2026-08-20**
- OANDA replaced by Saxo: OANDA closed its Maltese entity in 2023 and EU clients are
  served by OANDA TMS Brokers (Poland), which OANDA's own v20 docs exclude from API access.
- v1's claim that bid/ask candles give a real spread was true of LIVE but **not of SIM**,
  where the spread is a constant 2.0 pips.
- OANDA-style tick volume assumed as a volume proxy. Saxo has **no volume field at all**.

**2026-08-21 — from the live spike**
- **D-a §7.1/§4.2** — `Format.Decimals` is 4 and 2, not 5 and 3. `pip = 10**-Decimals`,
  asserted against `TickSize × 10`. v1's stated pip values were right; its derivation was
  wrong and fails by **10×** while looking plausible.
- **D-b §3** — refresh token lives ~1 h and **rotates**; no unattended credential exists at
  our tier. "Log in once, forever" is false.
- **D-c §3** — token exchange returns **201**; lifetimes vary (1070/1182/1200). Accept 2xx;
  read every operational value; assert the persist landed.
- **D-d §4.4/§5.2** — chart series **not stable across queries**: the same hour appears or
  vanishes by anchor and `Count`. Paging forbidden; Sunday week-open ambiguous.
- **D-e §4.4** — over-requested `Count` **silently clamps** to 1200. Assert the returned count.
- **D-f §7.6** — no `MarginRates`/`MarginTiers`. Leverage moves to config as a documented
  ESMA assumption.
- **D-g §5.3** — elevated spreads span **19:00–21:00 UTC**, not ±15 min. GBPUSD at 21:00
  costs **0.667R** on an 18-pip stop. Rollover gate becomes spread-triggered.
- **D-h §4.2** — chart v3 returns **null** `ChartInfo`/`DisplayAndFormat`; precision from
  reference data only. *(Corrected 2026-08-21: the spike recorded these as empty objects
  `{}`; the live re-check found them to be `null`. It changes nothing — they are never
  read, and precision comes from reference data precisely because there is nothing to
  read here — but the fixture now matches reality on a field the spec names.)*
- **D-i §12** — `MarketDataViaOpenApiTermsAccepted: False` while data flows. Added as a
  named potential pause cause, not a reliable gate.
- **D-j §4.4** — real tail lengths are **321/321/321/101**, not 201/201/201/101.
- **D-k §6.1/§4.4** — **4h and 1d bars are anchored to 17:00 America/New_York and shift
  with US DST.** The 1d `Time` is a date label, not a start time. §6.1's recommendation is
  **reversed** to native alignment.
- **§8** — the economic calendar assumption is **FALSE**. The YAML is the only source, and
  needs a staleness rail.
- **§7.4** — cost numbers corrected downward: 0.061R on an 18-pip EURUSD stop, not 0.11R.
- **§7.5** — new open question the spike surfaced by omission: which side of the spread
  features are computed from.

**2026-08-21 — from the M10b-1 build**

The spike checked this spec against the live API. These were found checking it against
the **code**, which is a different exercise and produced five more defects. Owner
rulings on all of them are dated the same day.

- **#12 §7/§11 — `TradePlan` cannot carry a forex plan.** It lives in the frozen
  `sentinel/risk/` and is crypto-shaped: `notional_usdt`, `suggested_leverage`,
  `liq_distance_pct`, `liq_buffer_ok`, and a `PlanCosts` carrying a perpetual-futures
  funding rate. `bot/cards.py` reads all four directly. §7.6 forbids faking a
  liquidation buffer, and the package may not be extended, so §7's implicit assumption
  that a forex plan could travel as a `TradePlan` is false. **Ruling:** M10b ends at a
  sizing result in a new `sentinel/fx/` package, with the stronger form of §2.1 applied
  — a concept this market does not have gets **no field at all**, not a null and not a
  zero. The card, publishing and tracking become **M10c**.
- **#13 §2.1 — "omit, never zero" had nowhere to land.** `Candle.volume` was a required
  Pydantic field and `ohlcv_candles.volume` was NOT NULL, so a forex candle could not
  express the absence the section demands. **Ruling:** migration `0011` drops the NOT
  NULL (catalogue-only, no row rewritten, no backfill), and the permission is policed in
  **both** directions in code — a crypto candle with no volume and a forex candle with
  one are each an error. "Nullable" quietly becoming "sometimes missing" for crypto would
  corrupt relative volume with nothing to notice it by.
- **#14 §5.1 — the stated premise is inverted.** This section says crypto's staleness
  rule "would fire on every weekend snapshot". It would not:
  `sentinel/ingestion/staleness.py` compares `OHLCVSeries.fetched_at`, which is always
  ~now for a fresh fetch, not the newest candle's `open_time`. The real hazard is the
  opposite — a weekend snapshot whose newest bar is fifty hours old would read as
  perfectly **fresh**. The conclusion §5.1 reaches is right and its reasoning is
  backwards: forex needs a **candle-recency** check crypto never needed, and that is the
  check which must be skipped while the market is closed.
- **#15 §5.3 — the spread gate's baseline was self-defeating.** Corrected in place above.
- **#16 §7.4 — net RR was computed the wrong way round.** Corrected in place above. The
  required uplift is ~2.5× what the section claimed.

Smaller corrections from the same build:

- **§4** — the adapter is `sentinel/ingestion/adapters/forex_saxo.py`, alongside the
  Binance one, not `sentinel/ingestion/forex_saxo.py`.
- **§4.2** — resolved instruments are cached in a new **`forex_instruments`** table, not
  in `instrument_meta`. That model is a tick size, a quantity step, a minimum *notional*
  and a contract size; forex has a Uic, a `Format.Decimals`, a pip and a minimum *trade
  size* in base units. Three of those have no column there and the fourth would put a
  units figure where a money one is read. Same defect class as #12.
- **§7.3** — the spread profile is **computed** from the 1h tail rather than stored in a
  table: no extra request, no extra migration, and the statistic stays a pure function of
  data the cycle already holds. The forex 1h tail is therefore **1200 bars, not 321**
  (owner correction, 2026-08-21): 321 is ~13 days and leaves ~10 samples per hour-of-day
  bucket, and a median over ten noisy samples is not a baseline — least of all in the
  tail hours where it decides whether a signal is emitted. 1200 is ~50 days and ~35
  samples, still one request, sitting exactly at the ceiling. Features continue to use
  the most recent 321 of that tail.
- **§7.1** — the *entry* and *stop* handed to the sizer are bid-series levels. The spread
  is charged as a cost, never shifted into the levels. Doing it the other way would move
  every level by the spread — one pip on EURUSD, twelve at rollover on GBPUSD.
- **Evidence — CLOSED 2026-08-21.** The spike's 89 raw JSON files were deleted along
  with its throwaway scripts (commit `4c90819`). Dropping the scripts was right; the raw
  responses were evidence and should have been kept, and every Saxo fixture in the suite
  therefore had to be **reconstructed from this document** — which meant a misreading in
  the spike would have reproduced into a fixture with nothing to catch it.
  `python -m sentinel.tools.saxo_record_fixtures --login` was run against the live API on
  2026-08-21 and **every asserted value matched**: Uics 21/31/42, `Format.Decimals`
  4/4/2, the derived pips, `TickSize`, `MinimumTradeSize` 1000.0 and the chart row keys.
  The fixtures are now **VERIFIED**, and their provenance headers say so with the date —
  still not captures, and a test keeps the two claims distinct.
- **§4.4 / D-d — SETTLED 2026-08-21, in our favour.** That a 1200-bar read reproduces a
  321-bar one was an expectation when the tail was widened. It has now been checked
  against the live API on all three pairs: the 1200-bar request returned **exactly**
  1200, every bar of the shared window agreed, and the `TimeframeFeatures` computed from
  the long tail's last 321 rows were **identical** to those from a direct 321-bar
  request. So the widening that gives the spread profile ~35 samples per hour-of-day
  instead of ~10 costs nothing in feature terms, and the anchor instability D-d found
  does not reach this boundary.

**2026-08-21 — from the M10b-2 build**

Four more, all found the way the M10b-1 five were: by reading this document against the
code that has to implement it. Owner rulings on all four are dated the same day.

- **#17 §6 — the session labels have no definition.** §6 asks for "session labels:
  Tokyo / London / New York / the London-NY overlap" and gives no hours and, worse, no
  timezone basis. **Ruling: DST-aware local windows** — Tokyo 09:00-18:00 `Asia/Tokyo`,
  London 08:00-17:00 `Europe/London`, New York 08:00-17:00 `America/New_York`, overlap
  = London ∩ NY, converted through stdlib `zoneinfo` and never written down as a UTC
  hour. This is D-k's lesson applied to sessions: the US and EU change daylight saving
  on different dates, so for about three weeks each spring and one each autumn the
  London-NY overlap is **five** hours rather than four. A config table of UTC hours
  would be right for forty-four weeks a year and silently wrong for the rest. New York's
  17:00 close is also the same instant Saxo anchors its daily bar to, and a test asserts
  the two agree in both seasons.
- **#18 §6 — the USD strength index and the correlation have no definitions either.**
  "A synthetic USD strength index from the three pairs" names no formula; "rolling
  cross-pair correlation" names no window, no timeframe and no return basis.
  **Ruling:** USD legs `1/EURUSD`, `1/GBPUSD`, `USDJPY`; index = equal-weight
  **geometric** mean of the legs, rebased to 100 at the window start, on 1h closes over
  the 321-bar feature window; correlation = Pearson on **log returns**, 120 bars,
  pairwise. Geometric because it is scale-free and symmetric — a leg halving and a leg
  doubling cancel exactly, which an arithmetic mean of percentage changes does not, and
  a test pins that property rather than a magic number. Log returns because two trending
  price series correlate near 1 whatever they are doing, which would make §9's rail look
  satisfied by arithmetic.
  Both are **cross-symbol**, which §6 does not acknowledge: `features.engine.compute()`
  is per-symbol and market-blind, so they are computed once per cycle in
  `sentinel/fx/features.py` and shared. Two thirds of a dollar index is not a dollar
  index — a missing pair yields `None`, never a partial one.
- **#19 §11 / ENSEMBLE.md §2 — the prompt filename convention has no market axis.**
  §2 fixes prompt filenames per *provider* (`fable_vN.md`), settled 2026-08-18. A forex
  prompt is a second axis. **Ruling: `fable_forex_v1.md`** — provider-major, market as
  suffix, extending to `sol_forex_v1.md`. `fable_v1.md` is untouched and is the crypto
  prompt. Selected through a new `MarketConfig.analyst_prompt_version`, following the
  `adapter: str` precedent in the same model.
- **#20 §6 — the chart annotations had nowhere to be recorded.** §6 asks for prior-day
  and prior-week levels and session shading on the chart. `ChartRenderParams` is the
  reconstruction record and is dumped whole into the crypto chart golden, so a new field
  would move crypto's bytes for a market that has none of these marks.
  Raised as "draw them now, record them at M10c". **Ruled against**, and the reason is
  worth keeping: a stored chart that draws a line nothing in the database explains
  breaks the property M3 exists for, and "forex reaches no signal row yet" is true only
  until the day it does — by which time the gap is in the code and nobody remembers it
  was deliberate. **Resolved by conditional serialisation**: `ChartRenderParams` gains
  `annotations`, and a `@model_serializer(mode="wrap")` omits the key entirely when it
  is empty. Crypto's `to_json_dict()` is byte-identical to what it was before the field
  existed; forex's carries every line and band it drew. Both halves are tested, and the
  test that the key is *absent* has a sibling proving it is not absent always.

**2026-08-21 — from the M10c build**

Three more, found the same way the last nine were: by reading this document against the
code that has to implement it. One of them is a defect in the **milestone instruction**
rather than in this spec, and it is recorded here because that is where somebody would
look for it. Owner rulings on all three are dated the same day.

- **#21 §7/§9 — the forex gate has no quality thresholds, and crypto's do not transfer.**
  §7 and §9 name the sizing rails and the correlation cap and say nothing about setup
  *quality*: no minimum net RR, no minimum confidence, no entry-distance bound, no
  ATR bounds on the stop, no cooldown, no daily cap. `ForexConfig` accordingly has none,
  so the obvious move is to read crypto's `RiskConfig` — and one of those numbers is
  actively wrong here. `max_entry_distance_pct: 3.0` bounds how far an entry may sit from
  the last price; EURUSD moves about **0.5% in a day**, so a 3% bound can essentially
  never fire. A rail that cannot fire is worse than an absent one, because it reads on a
  checklist as a rail. **Ruling: forex gets its own thresholds in `ForexConfig`** —
  `min_rr_tp1`, `min_confidence`, `max_entry_distance_pct`, `stop_atr_min_multiple`,
  `stop_atr_max_multiple`, `signal_cooldown_hours`, `max_signals_per_day` — each a
  starting guess to be calibrated from DRY_RUN, in the same standing as
  `spread_max_multiple: 3.0`, and crypto's `risk:` block untouched.

- **#22 §11 — capital renders at eighteen decimal places, and no golden could see it.**
  §11 says M10a did the surface work and M10b consumes it. Consuming it surfaced a live
  defect in the crypto card: `capital_eur` and `risk_per_trade_pct` are the only two
  `TradePlan` fields assigned without `money()` or `percent()` (`risk/engine.py`), and
  both originate in `Numeric(38, 18)` columns, so Postgres hands back
  `Decimal('200.000000000000000000')` and the card prints it. The golden cannot catch it
  because the golden's capital is an in-process `Decimal("10000")` — scale 0, never
  round-tripped through the database — so `card.txt` reads `capital €10000` and stays
  green with the bug present. This is the same class as the `$10` → `$10.0` regression
  `tests/golden/test_golden_surfaces.py` already names: a `Decimal` **scale** difference
  where equality says the two agree and the rendered text does not.
  **Ruling: fix it at the render seam, not at the source.** The architecturally correct
  place is `risk/engine.py`; that package is frozen for the live measurement window, and a
  display-scale fix is not worth spending the freeze on. `bot/formatting` gains a money
  formatter, and the no-arithmetic guard gains a **numeric** rather than string comparison
  plus a new rule it can enforce: the renderer may fix a display *scale* and never a
  *value*. That missing rule is why the defect lived.

- **#23 §11 — "regenerate at switch-on" is unexecutable inside a milestone that keeps
  forex off.** This is a correction to the **milestone instruction**, not to §11's
  reasoning, and the distinction matters. §11 is right that switching forex on gives
  crypto cards their market tag and changes their bytes. The M10c brief then required that
  regeneration to happen "HERE, as an explicit numbered step" **and** required
  `markets.forex.enabled: false` in the same breath. Both cannot hold: with one market
  enabled `AppConfig.multi_market` is `False`, `section_header` returns `""`, and no
  crypto card byte can move. There was nothing to regenerate.
  **Ruling (owner, 2026-08-21): pin the tagged form as an ADDITION instead.** A
  multi-market golden set is rendered now from the shipped config with forex flipped on in
  memory, written by its own generator with its own hard write-allowlist, following
  `generate_goldens_m10b2.py`. The 27 existing fixtures are not regenerated for the tag at
  all. Switch-on day therefore becomes a config flip with **zero** golden churn, rather
  than a regeneration performed on the one day there is least attention to spare for it —
  and `test_the_deployed_config_renders_the_same_surface`, the strongest assertion in the
  suite, keeps holding because the shipped config is still single-market.
  The only bytes M10c regenerates are the two lines defect #22 fixes.

**2026-08-21 — from the M10d switch-on**

Six more, and the shape of them is different from the twelve before. Those were found
by reading this spec against the code. **These were found by composing the joins M10c
left open and by turning the flag on** — five of the six were in code this document
already described as done, and every one of them was invisible while forex was
disabled. Owner rulings on all six are dated the same day.

- **#24 §3.1 — the OAuth chain had three unwired callers out of four.** ``SaxoAuth``
  itself is correct and has been since M10b. Nothing called ``bootstrap()``, so
  ``SAXO_REFRESH_TOKEN`` never left ``.env`` and the token store was **empty at boot** —
  forex would have died on its first cycle with a valid credential in the environment.
  Nothing called ``ensure_fresh()``, so §3 requirement 5's five-minute cadence did not
  exist and the token was touched once an hour by a scan, against a credential that
  lives about an hour. And ``reauth_alert()`` was constructed only in a test, with
  ``ReauthenticationRequired`` caught nowhere that could send a message. **Ruling:**
  ``sentinel/core/forex_auth.ForexCredentialKeeper`` is those three callers and holds no
  rules of its own; the alert is keyed on the dead credential's **fingerprint** rather
  than the date, so a second death after a fresh login is not suppressed. §3.1 is also
  reframed above — the one-hour memory is a rare-event cost, not constant toil.

- **#25 §16.10 — the forex tracker job could not build its adapter.** ``app.track_for``
  went through ``wiring.market_adapter``, which supplies neither an HTTP fetcher nor an
  access-token provider; ``wiring._saxo`` raises ``UnknownAdapter`` without both. Binance
  needs neither, so crypto has been fine since M7. The exception was swallowed into a
  ``scheduler.tick_failed`` log line, so ``tracker:forex`` would have failed every 60
  seconds for ever with ``/health`` green. **Ruling:** forex builds through
  ``wiring.forex_adapter``, and ``_schedule_pipeline``'s closure is now *executed* in a
  test rather than only counted.

- **#26 §4.1 — the closed-candle rule is right for indicators and wrong for the
  tracker.** ``PriceFeed`` asks one method two different questions and Binance answers
  both correctly by returning the in-progress candle last. Saxo has no closed flag, so
  the adapter applies §4.1 itself — which left the 1m fill read **120 s stale on every
  tick** (M7 reads 1m high/low precisely so a 60-second poll cannot miss a wick) and made
  the invalidation read drop the forming bar **twice**, discarding the newest closed
  hour, which is the only hour an invalidation is ever measured on. **Ruling:** §4.1's
  rule stands unchanged for anything computing an indicator, and
  ``sentinel/fx/pricing.ForexCandleSource`` answers each of ``PriceFeed``'s two questions
  separately. A high and a low are prices already traded; an EMA is not.

- **#27 §2.1 — an empty section is an HTTP 400, not an empty section.** The forex path
  passed ``""`` as its history block and ``user_blocks`` appended it unconditionally. The
  Messages API refuses an empty text block outright, so **every forex analyst call would
  have failed** — as an ``AnalystUnavailable`` that reads in the logs like a vendor
  problem rather than like a bug. Nothing caught it because the forex prompt had never
  once been sent. Verified against the live endpoint before the fix. **Ruling:** the
  forex path builds a real history block through the same market-scoped
  ``_history_block`` crypto uses, and ``user_blocks`` drops an empty block so the failure
  is impossible rather than merely absent.

- **#28 §11 — "switch-on moves no golden" was half true.** M10c pinned the tagged bytes
  in advance and that half held perfectly. What it could not pin is that
  ``render_surfaces()`` defaults to ``load_config()``: once the shipped config enabled
  both markets it rendered the **tagged** form into the untagged fixtures, and
  ``test_every_multi_market_surface_differs_by_its_header_alone`` compared a string with
  itself. **Ruling:** ``single_market()`` mirrors ``multi_market()``; the single set
  renders from it and the multi set from the shipped config. No fixture's contents moved
  for the tag — the mapping moved.

- **#29 §7.4 / §16.11 — the cost model omitted prompt caching, and forex has neither of
  crypto's cost controls.** Measured (journal/M10d_REPORT.md): one call is **$0.226104**
  cached and **$0.281925** uncached, and a cycle of three pairs pays one cache write and
  two reads = **$0.734**, not the ~$1.00 a naive three-times-one estimate gives. The
  larger point is structural and §16.11 should have said it: forex has **no screener and
  no usable re-analysis cooldown**, so every pair buys a full analyst call every cycle.
  A cooldown of one setup-timeframe candle saves nothing here, because forex's setup
  timeframe is **1h** and the scan interval is 60 minutes — the cooldown would expire
  exactly when the next scan fires. **Ruling:** a config-level scan window
  (``forex.scan_hours_utc``, 07:00–19:00 UTC) is the cost control for the observation
  window; a forex screener is deferred until there is data to tune it against.

- **#30 §5.3 — the rollover rail cannot fire, and the window it was written for is
  uncovered.** The spread-triggered design keeps a clock floor "for when the spread
  series is unavailable"; the series is never unavailable at a 1200-bar tail, so
  ``ROLLOVER_WINDOW`` is unreachable in production and the measured rail decides the
  rollover hours alone — admitting every one of them except GBPUSD at 21:00, because
  3.0× the global median is 3.3/5.4/4.5 pips against 19:00–20:00 hour medians of
  1.1–1.8 and 1.5–4.6. Corrected in place in §5.3 with the full table. **Ruling: not
  fixed here.** Every candidate fix is a new uncalibrated threshold, and
  ``forex.scan_hours_utc`` ending at 19 — already ``friday_signal_cutoff_hour_utc`` —
  closes the exposure for the observation window without inventing a number. Two tests
  pin it: one that the backstop cannot fire (to be inverted, not deleted, when somebody
  fixes it) and one that the scan window ends at or before the rollover band, so the
  exposure cannot be re-acquired by widening the window alone.

### The M10b boundary defect — a milestone boundary is untested by construction

**M10b-1 shipped a Saxo adapter whose output could not be drawn, and every one of its
1777 tests passed.** `OHLCVSeries.to_frame` puts NaN in the volume column for a market
with no volume; `charts/renderer._draw` passed `volume=True` unconditionally; mplfinance
raised `ValueError('Axis limits cannot be NaN or Inf')` on the first forex render.

Neither half was wrong. The adapter was tested, the renderer was tested, and **the join
between them was not**, because the renderer belonged to the next session. Splitting
M10b in two created a seam and the defect lived exactly in it.

This is the same family as journal/M8_REPORT.md's "silence-as-success" and
journal/M10b_REPORT.md §3b's `alembic upgrade head` exiting 0 having done nothing: a
positive signal that was real and did not mean what it was taken to mean. Here the
signal was a green suite, and what it actually meant was "every piece works alone".

**The rule to carry forward: when a milestone boundary splits a producer from its
consumer, the next milestone composes them first and builds second.** M10b-2 does that
in `tests/core/test_forex_cycle.py`, which drives the real adapter over a synthetic
venue into the real renderer and looks at the bytes.

M10b-2's own boundary with M10c leaves four joins untested for the same reason, named
here so they are the first thing M10c composes: a forex `AnalystReport` has never
reached `bot/cards.py`; a forex `ChartRenderParams` has never been persisted into a
`signals.chart_params` row; `ForexSizing` has never been fed levels from a real analyst
report; and `TrackerLoop` has never seen a forex symbol.
