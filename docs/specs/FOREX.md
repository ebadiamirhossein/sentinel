# FOREX.md — the forex market (M10b)

Status: **draft for review.** Written 2026-08-20, after M10a shipped and after the
Saxo OpenAPI was tested against a live simulation account.

M10a taught Sentinel to hold more than one market. M10b adds the second one.

This document specifies the forex market end to end: the adapter, the data it can
and cannot give us, the sizing arithmetic, the costs, the trading calendar, and the
rails. It does not specify the ensemble (M11/M12) or the dashboard (M13).

---

## §0 — Scope and the one hard rule

**In scope:** EURUSD, GBPUSD, USDJPY, sourced from Saxo Bank OpenAPI, delivered as
manual-execution trade plans through the existing Telegram bot, sized in EUR,
measured in its own DRY_RUN population.

**Out of scope for M10b:** gold and other metals, indices, additional pairs, any
form of execution, and any change to crypto.

**The hard rule, carried forward from M10a:** crypto's behaviour does not change.
`sentinel/risk/` stays frozen — forex sizing goes in a **new module**, not an edit to
the existing one. The golden cycle and golden surface tests from M10a are the gate:
they must stay byte-identical throughout M10b. If a forex change moves a crypto
golden, the change is wrong.

Forex ships with `markets.forex.enabled: false` and turns on later by config edit,
starting in `dry_run: true` while crypto stays live.

---

## §1 — Provider: what we verified, and what we only assume

On 2026-08-20 a real request was made against Saxo's simulation environment:
`GET /chart/v3/charts` with `AssetType=FxSpot, Uic=21, Horizon=60, Count=10`.
It returned HTTP 200 with ten EURUSD hourly candles.

The distinction below matters more than anything else in this document. Claude Code
must treat **VERIFIED** as fact and **ASSUMED** as something to prove during M10b —
and if an assumption turns out false, stop and report rather than working around it.

### VERIFIED

| Fact | Evidence |
|---|---|
| The chart endpoint works and returns real hourly candles | HTTP 200, ten candles, plausible EURUSD values around 1.168–1.171 |
| Every candle carries **both bid and ask** for O/H/L/C | `OpenBid`/`OpenAsk`/`HighBid`/`HighAsk`/`LowBid`/`LowAsk`/`CloseBid`/`CloseAsk` all present |
| Timestamps are UTC, ISO-8601, on the hour | `"Time": "2026-08-20T07:00:00Z"` |
| There is **no volume field of any kind** | Absent from the response — not zero, absent |
| There is **no "candle closed" flag** | No such field; the newest candle was mid-formation with a visibly compressed range |
| The SIM spread is **synthetic and constant** | Exactly 0.00020 on every field of every candle, all day |
| A live (unfunded) account can reach the developer portal | Live Apps login succeeded |
| A read-only application can be created | Live app created with the trading checkbox left unchecked — the app is structurally unable to place an order |

### ASSUMED — prove these early in M10b

- That the **live** environment returns a real, varying spread rather than the SIM's
  fixed 2.0 pips. This is the single most valuable assumption to test, because the
  entire cost model rests on it (§7). Test it on day one: pull the same instrument
  across a London-open hour and a rollover hour and compare.
- That live forex chart data is available without a paid market-data subscription.
  Saxo's public documentation states forex prices and forex chart data are free by
  default; that has not been confirmed on this specific account.
- That the reference-data endpoint returns `MinimumTradeSize` and decimal precision
  per instrument (§4.2).
- That an economic calendar is reachable through the same API (§8).
- That an unfunded live account keeps API access indefinitely. It works today. If it
  stops, forex degrades to paused and the owner is alerted — it must never take
  crypto down with it.

### Why Saxo and not a data vendor

For a system that emits exact entry ladders and stops which a human executes at a
specific broker, the analysed price series and the executed price series should be
the same series. A consolidated third-party feed would be a *different* series at any
price. Saxo is both the venue and the feed.

Secondary reasons: forex chart data costs nothing on the account, the API is REST
with a maintained Python wrapper, and news plus an economic calendar arrive through
the same authenticated channel instead of three more dependencies.

---

## §2 — The honest ledger: what forex loses, and what replaces it

Crypto gives the analyst six inputs forex cannot.

| Lost | Replacement | Status |
|---|---|---|
| Funding rate | Swap/rollover from a config table, charged 21:00 UTC, **triple on Wednesday** | §7.3 |
| Open interest | **Nothing.** Tell the analyst it is unavailable | §2.1 |
| Long/short ratio | **Nothing.** Saxo's order-book and position-book endpoints were discontinued | §2.1 |
| Real volume | **Nothing from this endpoint.** Not even tick counts | §2.1 |
| Exchange order-book depth | Measured spread series — *if* §1's assumption holds | §7.1 |
| 24/7 trading | Session structure: Tokyo / London / New York / the London–NY overlap | §5 |

And four things forex offers that crypto does not, all of which become **deterministic
features computed by us**, never prompt hints:

- **Prior-day high/low, prior-week high/low, daily open, weekly open.** Reference
  levels that matter far more in FX than in crypto.
- **A synthetic USD strength index**, computed from the three pairs we already fetch.
  Deliberately not DXY: DXY is ICE-licensed and redistribution is a problem we have no
  need to acquire.
- **Rolling cross-pair correlation**, which feeds a new portfolio rail (§9).
- **Scheduled economic events.** In crypto a nice-to-have; in forex a hard requirement,
  because a CPI print is a scheduled gap (§8).

### §2.1 — Missing data must look missing

Where an input does not exist, the feature must be **absent**, not zero, not a
placeholder, and not silently omitted from the prompt. The analyst prompt states
plainly which inputs are unavailable for this market.

This is the project's oldest lesson. A zero that means "no data" reads to a model as
"no activity," and produces a confident answer built on nothing. There is a test for
this: assert that the assembled forex prompt contains the explicit unavailability
statement, and that no forex feature dict contains a zero standing in for a null.

---

## §3 — Authentication

Saxo uses OAuth 2.0 authorization-code flow. The developer portal's 24-hour token is
for development only and must not appear in any deployed path.

**Flow:** the owner completes the login **in a browser on his own Mac**, the code
lands on `https://localhost:8080/callback`, Sentinel exchanges it for an access token
and a refresh token, and the refresh token is copied once into the server's `.env`.
The server then refreshes on its own and never opens a browser.

This preserves a deliberate property of the deployment: **Sentinel needs no inbound
port.** Telegram uses long polling, health binds to loopback, Postgres publishes
nothing. A public OAuth redirect would have required opening a port through Caddy for
a login that happens roughly never.

**Requirements:**

1. Read the token lifetimes from the token response. **Do not hardcode them** — they
   are a vendor's operational detail, not a constant.
2. Persist the refresh token in Postgres, not in a file. It must survive a container
   restart and a redeploy.
3. Refresh proactively, well before expiry, with jitter and bounded retry.
4. On refresh failure: **pause the forex market only**, alert the owner in Telegram
   with a clear cause, and leave crypto entirely untouched. There must be a test that
   forces a refresh failure and asserts a crypto cycle still completes.
5. The credentials never appear in logs, in a process listing, or in an error message.
   `ops/lib.sh` already refuses passwords on a command line; the same standard applies.

---

## §4 — The adapter

New module `sentinel/ingestion/forex_saxo.py`, implementing the existing
`MarketDataAdapter` protocol. Registered in `core/wiring.py` under the adapter key
`forex_saxo`, which M10a deliberately left unregistered.

### §4.1 — Candle completeness (the sharpest edge in this milestone)

Saxo's response carries **no closed flag**. Binance's does. If the forming candle is
fed into RSI, ATR or the EMA stack, every indicator is wrong on the most recent bar —
and wrong in a way that produces plausible numbers and no error at all.

**Rule:** a candle stamped `T` with horizon `H` minutes is closed if and only if
`now >= T + H + grace`, where grace is a small configured margin. The newest candle is
dropped unless it satisfies this. The tail is measured in **closed** candles only.

This gets its own test with a frozen clock, exercising the boundary directly: one tick
before the close, exactly at it, and one tick after.

### §4.2 — Instrument resolution: never hardcode a Uic

Saxo identifies instruments by an internal numeric `Uic`. EURUSD happens to be 21.
**No Uic is hardcoded.** They are resolved at startup from the reference-data endpoint
by keyword, and cached in `instrument_meta` — which gained a `market` column in M10a.

The same lookup supplies `MinimumTradeSize`, decimal precision and the display format.
This is the direct descendant of the crypto adapter reading `exchangeInfo` for tick
size and min notional instead of guessing: **let the venue tell us its own rules.**

If an instrument cannot be resolved, that symbol is skipped with a named reason. It is
never silently dropped.

### §4.3 — Symbol namespace

Forex symbols are the six-character pair with no separator: `EURUSD`, `GBPUSD`,
`USDJPY`.

M10a flagged that `ohlcv_candles` keeps its primary key of
`(symbol, timeframe, open_time)` while carrying a `market` column, so two markets
sharing a symbol string would collide. `BTCUSDT` and `EURUSD` are disjoint, so there
is no problem today. M10b adds a **startup assertion** that no enabled forex symbol
equals any enabled crypto symbol, so the day a namespace overlaps we find out loudly
rather than through corrupted candles.

### §4.4 — Timeframes

Mirror crypto exactly: the same timeframe set, the same tail lengths (201/201/201/101),
so the feature engine needs no forex-specific branch. Saxo's `Horizon` is expressed in
minutes and covers every value we need.

If a tail exceeds the documented maximum candles per request, page it. Read the
documented maximum rather than assuming one.

---

## §5 — Market hours, sessions and the weekend

### §5.1 — Closed is not degraded

`market_hours()` already exists in the adapter protocol and returns "always open" for
crypto. For forex it returns the real session state.

**The trap:** crypto's staleness rule — OHLCV older than 2× the timeframe means
DEGRADED, skip the symbol — will fire on every weekend snapshot and fill the logs with
false failures.

**Market hours are checked before staleness.** A closed market is a normal state with
its own reason code, not a data problem. This has a test.

### §5.2 — The trading week

Roughly Sunday 21:00 UTC to Friday 21:00 UTC — and **that boundary moves by an hour
twice a year**, because the US and the EU change daylight saving on different dates.

Do not hardcode it. Derive the session from actual candle availability, with the
nominal times in config as a sanity check. A divergence between the two is worth
logging: it means either DST moved or the venue changed its hours.

### §5.3 — Rollover

At 21:00 UTC the spread widens sharply and swap is charged. No new signals within a
configured window either side (default ±15 minutes).

### §5.4 — The weekend gap — a failure mode crypto never had

A pending entry ladder cannot fill over a weekend. A filled position gaps through its
stop on Monday. Neither has any analogue in a 24/7 market, and the tracker has never
had to reason about it.

**Rules:**

- No new signals after a configured Friday cutoff.
- **All pending entry ladders expire before the Friday close.** Not paused — expired,
  with an explicit reason the owner sees.
- Any position still open into the weekend gets a clear weekend-gap warning on the card.
- Cycles are skipped entirely while the market is closed. This also saves LLM spend.
- The tracker idles rather than erroring, and does not interpret a frozen price as a
  stall.

---

## §6 — Features and charts

The feature engine is shared. Forex adds computed features and omits impossible ones.

**Added:** prior-day high/low, prior-week high/low, daily open, weekly open, session
labels, the synthetic USD strength index, rolling cross-pair correlation, and the
measured spread series (§7.1).

**Omitted:** every volume-derived feature. Omitted, not zeroed (§2.1).

**Charts** use the same renderer with a forex variant: no volume panel at all rather
than an empty one, prior-day and prior-week levels drawn, session shading, and no
funding annotation.

### §6.1 — Daily candle alignment — OPEN DECISION

FX convention closes the day at 17:00 New York. Pure UTC is reproducible and does not
drift with daylight saving. This choice decides what "yesterday's high" means, and
therefore what a level is.

**Recommendation: UTC**, for determinism and because every other timestamp in the
system is UTC. The trade-off is real — it means our daily levels differ from what most
FX charts show. Owner's call.

---

## §7 — Sizing and cost: the FX risk module

A **new module**, `sentinel/risk/forex/` or equivalent, built **test-first from
hand-computed fixtures**, exactly as M4 was. `sentinel/risk/`'s existing contents are
not edited. Its 100% branch coverage must remain unchanged.

This is where the next FX-inversion-class bug will hide, so the general form is
specified here rather than left to be rediscovered.

### §7.1 — The sizing formula

```
units          = risk_eur × rate(EUR→quote_ccy) / stop_distance_in_quote
notional_eur   = units × price_quote / rate(EUR→quote_ccy)
```

`rate(EUR→quote_ccy)` is how many units of the quote currency one euro buys.

| Instrument | Quote | rate(EUR→quote) | Source |
|---|---|---|---|
| EURUSD | USD | EURUSD (~1.17) | Frankfurter, already in the system |
| GBPUSD | USD | EURUSD (~1.17) | Frankfurter |
| USDJPY | JPY | **EURJPY** (~170) | Frankfurter |

Note that USDJPY needs EURJPY, not EURUSD. Frankfurter already provides EUR-based
rates for USD, GBP and JPY, so no new dependency is required.

**Pip definitions are per instrument.** 0.0001 for EURUSD and GBPUSD; **0.01 for
USDJPY**, which quotes to three decimals. Do not derive this from a rule of thumb —
take precision from reference data (§4.2).

### §7.2 — The minimum-ticket problem, with real numbers

Using tonight's actual EURUSD rate of 1.169, €200 capital and 0.75% risk (€1.50):

| Stop | Ideal units | vs a 1,000-unit minimum |
|---|---|---|
| 18 pips | **974** | just below — rounds up to 1.027× intended risk |
| 25 pips | **701** | well below minimum |
| 40 pips | **438** | far below minimum |

Note the direction: **a wider stop needs a smaller position**, so wider stops make
this worse, not better.

Turned around: at a 1,000-unit minimum and 0.75% risk, the minimum viable capital is
about **€205 for an 18-pip stop** and about **€456 for a 40-pip stop**.

Two consequences, both of which must be stated in the M10b report rather than papered
over:

1. At €200, only tight-stop setups can be sized correctly, and tight stops are exactly
   the ones spread damages most (§7.4).
2. The engine must return an explicit `BELOW_MIN_TICKET` rejection rather than
   rounding silently. How often that fires is itself a useful measurement.

**Never round up into a larger position than the risk budget allows.** Under-risking is
acceptable; over-risking is not.

### §7.3 — Costs

Three components, all of which feed the existing net-RR gate:

- **Spread.** Measured per instrument per hour-of-day from the bid/ask candles — *if*
  §1's assumption about live data holds. If the live spread proves synthetic like the
  SIM's, fall back to a configured table and **label it an assumption, not a
  measurement**, everywhere it is surfaced.
- **Commission.** From config, per instrument, in the venue's own units.
- **Swap/rollover.** From config, per instrument per direction, charged at 21:00 UTC
  and **tripled on Wednesday**. Included for the expected holding period.

### §7.4 — What this does to the gate, stated plainly

Cost in R is roughly `spread_pips / stop_pips`. At the SIM's 2.0 pips on an 18-pip
stop that is **0.11R** — so a gross 1.5 RR becomes roughly 1.39 net, and fails the
existing `min_rr_tp1` of 1.5.

**Expect the net-RR gate to reject most tight-stop intraday forex setups.** That is
correct behaviour, not a bug, and it is far better to know it now than to discover a
suspiciously quiet forex market in three weeks and start loosening rails to explain it.

If live spreads turn out tighter than the SIM's 2.0 pips, this improves. If they turn
out wider at the times we trade, it gets worse. Either way it is measured, not assumed.

### §7.5 — Margin replaces the liquidation buffer

CFD margin is an account-level concept. There is no per-position liquidation price to
keep a buffer away from, so the crypto liquidation-buffer rule does not apply and must
not be faked.

Replace it with:
- required margin = notional / the venue's maximum leverage for that instrument
  (ESMA caps retail majors at 30:1; read the venue's actual figure where available)
- a rail on total forex margin as a share of equity
- explicit card text stating that stop-out risk is account-level, not per-position

---

## §8 — Economic calendar and blackouts

**Primary source:** Saxo's own calendar service, if it returns usable data with impact
levels — same authentication, no new dependency. **Verify this early**; it is listed
among the account's available features but has not been tested.

**Backstop, regardless:** a hand-maintained YAML of central-bank meeting dates. A feed
outage must not silently remove blackouts. An empty calendar response is treated as a
**failure**, not as "no events today" — the difference between those two is the whole
lesson of this project.

**Policy — OPEN DECISIONS below, with recommendations:**

- Suppress new signals from −60 to +30 minutes around high-impact events, filtered to
  the currencies in the pair. *(Recommended default.)*
- **Cancel pending entry ladders** for the affected currency ahead of high-impact
  events. An unfilled limit order sitting in front of a CPI print is a coin flip.
  *(Recommended; owner's call.)*
- Annotate open positions with a warning rather than closing them. Sentinel never
  instructs; it informs.

---

## §9 — Portfolio rails: correlation

EURUSD, GBPUSD and USDJPY all cross the dollar. Long EURUSD plus short USDJPY is one
large short-dollar bet wearing two hats, and the existing rails — which count positions
and risk without regard to what they are correlated with — would happily allow both.

**Recommendation for v1: a hard cap of one open forex position at a time.** Crude,
safe, and it makes the first measurement window interpretable. Relax to a proper
correlation-adjusted rail once there is data to calibrate against.

Forex rails are entirely separate from crypto's. An open crypto position does not
consume a forex slot, and vice versa. The per-market pause rails from M10a already
support this.

---

## §10 — News

CryptoPanic is crypto-only and is not used here.

**Primary:** central-bank RSS — Federal Reserve, ECB, Bank of England, Bank of Japan.
Official, free, high signal, low volume. **Secondary:** Saxo's news service if it
proves useful.

The `untrusted_news_data` delimiter defence from M5 applies unchanged. External text
is data, never instruction.

---

## §11 — Telegram, stats and populations

M10a did this work. M10b consumes it.

- Forex signal cards are tagged with their market, using the `multi_market` condition
  M10a introduced. With forex enabled, crypto cards gain their tag too — and that
  changes crypto's card bytes, so **the golden surfaces must be regenerated
  deliberately at the moment forex is switched on**, not quietly during the build.
  Note this explicitly in the M10b report.
- `/stats`, `/pulse`, `/positions`, `/watchlist`, `/journal` already section per market
  and never merge figures.
- Populations are REAL / HYPOTHETICAL / DRY_RUN × market, and never merge. `summarize()`
  already raises on rows spanning more than one market.
- **Forex starts in DRY_RUN** and stays there until the numbers justify otherwise.

---

## §12 — Failure modes

Every one of these degrades **forex only**. Each needs a test asserting a crypto cycle
completes normally while forex is in that state.

| Failure | Behaviour |
|---|---|
| OAuth refresh fails | Pause forex, alert owner, crypto untouched |
| Chart endpoint errors or times out | Skip symbol with a named reason; repeated failures pause forex |
| Instrument resolution fails | Skip that symbol, named reason, never silent |
| Calendar feed unavailable | Fall back to the YAML; if that is also missing, **suppress signals** rather than trade blind |
| Frankfurter unavailable | Existing last-known-good logic; if too stale, no sizing, so no signal |
| Market closed | Normal state, cycle skipped, no error |
| Forex sub-budget exhausted | Forex pauses, crypto continues (M10a, already tested) |

---

## §13 — Open decisions for the owner

Each has a recommendation. "Defaults for all" is a valid answer.

1. **Daily candle alignment** (§6.1) — UTC or 17:00 New York? *Recommend UTC.*
2. **Blackout cancels pending ladders?** (§8) *Recommend yes for high-impact events.*
3. **Blackout window** (§8) — *Recommend −60 / +30 minutes, currency-matched.*
4. **Max concurrent forex positions** (§9) — *Recommend 1 for the first window.*
5. **Forex capital** (§7.2) — €200 makes only tight-stop setups sizeable. Keep €200 and
   accept a high `BELOW_MIN_TICKET` rate as data, or raise it? *Recommend keeping €200:
   forex starts in DRY_RUN, where the sizing is measured but nothing is at stake.*
6. **Crypto budget floor** (carried from M10a's R5) — with crypto 10.00 + forex 4.00
   under an 11.00 ceiling, a forex-heavy morning can leave crypto short of its own
   budget once forex is live. Should crypto hold a reserved floor so a new, unmeasured
   market can never squeeze the measured one? *Recommend yes — a reserved floor for
   crypto of 8.00.*

---

## §14 — Suggested build order

M10b is large. Sequence it so each step is provable before the next depends on it.

1. **Spike, half a day.** OAuth end to end; live spread vs SIM spread; calendar
   endpoint; reference-data fields. Report findings **before** building. Several
   assumptions in §1 may turn out false, and it is cheaper to learn that now.
2. **Adapter**, with candle completeness and instrument resolution tested first.
3. **Market hours**, before anything consumes forex data, so staleness never misfires.
4. **FX sizing module**, test-first, hand-computed fixtures, `sentinel/risk/` untouched.
5. **Costs and net-RR**, with the measured-vs-assumed distinction visible in output.
6. **Calendar and blackouts.**
7. **Features and charts.**
8. **Wiring**, still behind `enabled: false`.

Crypto goldens run at every step. Nothing turns on until the owner says so.

---

## §15 — Exit criteria

- `markets.forex.enabled: false` in the shipped config; deploying M10b is a **no-op**
  for the running system.
- Crypto goldens byte-identical; `git diff sentinel/risk/` empty.
- `make check` green; migration verified against a fresh production dump.
- The FX sizing module has hand-verified fixtures for all three pairs, including
  USDJPY's EURJPY conversion and its 0.01 pip.
- Candle-completeness boundary tested with a frozen clock.
- Every §12 failure mode has a test proving crypto survives it.
- `journal/M10b_REPORT.md` records: which §1 assumptions held and which did not, the
  measured live spread, the `BELOW_MIN_TICKET` rate at €200, and every spec defect
  found during the build — dated, in this project's convention.

---

## Corrections log

- **2026-08-20** — OANDA was the original provider recommendation. It is unavailable:
  OANDA closed its Maltese entity in 2023 and EU clients are served by OANDA TMS
  Brokers (Poland), which OANDA's own v20 documentation excludes from API access.
  Saxo Bank replaces it.
- **2026-08-20** — An earlier draft claimed Saxo's bid/ask candles provide a real
  measured spread. On the **simulation** environment they do not: the spread is a
  constant 2.0 pips. Whether live data differs is untested (§1).
- **2026-08-20** — An earlier draft assumed OANDA-style tick volume would be available
  as a volume proxy. Saxo's chart response carries **no volume field at all**. There is
  no volume signal in forex.
