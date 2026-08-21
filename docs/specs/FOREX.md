# FOREX.md — the forex market (M10b)

Version 2, revised 2026-08-21 from the spike findings. Supersedes v1 (2026-08-20).

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

### §3.1 — The one-hour memory is a real operational cost

The refresh chain remembers for one hour. **If Sentinel is down longer than that, the
refresh token is dead and the owner must complete a browser login by hand.**

That covers: a long deploy, a server reboot, a Docker upgrade, any outage over an hour.
There is no way to engineer around it at this account tier.

So the alert must **name re-authentication as the action and carry the authorize URL**.
A generic "forex paused" sends the owner hunting a data problem when the fix is a
two-minute login.

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
