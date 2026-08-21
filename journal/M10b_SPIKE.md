# spike/FINDINGS.md — Saxo LIVE, verifying FOREX.md §1

**Run date:** 2026-08-21, 07:00–07:30 UTC (Friday), against **LIVE**
(`https://gateway.saxobank.com/openapi`), account `22611125`, unfunded, app created
with the trading checkbox unchecked.

**Clock, verified three ways before any date-dependent probe** (host `date -u`, an
independent `datetime.now(UTC)`, session context): **Friday 2026-08-21**. The weekend
probed is therefore **Sat 2026-08-15 / Sun 2026-08-16**, nominal closed window
`2026-08-14T21:00Z → 2026-08-16T21:00Z`, derived at runtime and never written as a
literal. A mid-run claim that the date was Thursday 2026-08-20 was checked and is
inconsistent with all three sources; the `2026-08-20T16:00:00Z` candle behind it came
from the previous day's SIM probe.

**Scope discipline:** read-only throughout. No order endpoint appears anywhere in
`spike/`. The single `/trade/` call is `infoprices`, a quote-retrieval endpoint,
approved by the owner as the control for item 3.

Raw evidence: `spike/raw/*.json` (89 files). Credentials are the only redaction;
token values appear as sha256 prefixes so two runs can be compared without exposure.

---

## Summary — the four things that change the plan

1. **The live spread is real and it varies.** 1.0–18.3 pips on EURUSD across 1200
   hourly candles, median 1.1. The SIM's constant 2.0 was synthetic. §7.3 measures.
2. **Reference data contradicts §7.1 on decimals.** `Format.Decimals` is **4** for
   EURUSD/GBPUSD and **2** for USDJPY — not 5 and 3. The pip is `10**-Decimals`.
3. **"Log in once, forever" is false.** The refresh token lives ~1 hour and rotates
   on every use. Any outage longer than an hour requires a manual browser login.
4. **The chart series is not stable across queries.** The same hour is present or
   absent depending on the request's anchor and `Count`. This is a paging hazard.

---

## 1. OAuth end to end — VERIFIED (with two defects found)

**Tested:** full authorization-code flow on LIVE, then a refresh.

### Lifetimes, verbatim from the responses

| response | HTTP | `expires_in` | `refresh_token_expires_in` |
|---|---|---|---|
| exchange (earlier attempt) | 201 | **1070** | (redacted by the then-current scrubber) |
| exchange `authorization_code` | **201** | **1182** | **3582** |
| `refresh_token` | 200 | **1200** | **3600** |

`token_type: Bearer`, `base_uri: null`. No other fields.

### Rotation — sha256 prefixes, values never compared in the clear

```
refresh before = sha256:4583b3457181bbff len=36
refresh after  = sha256:31b9e19b31f809bb len=36
REFRESH TOKEN ROTATED: True
access  before = sha256:51c932689c5a5069 len=514
access  after  = sha256:3d9de60591c98fe6 len=514
ACCESS TOKEN ROTATED:  True
```

**VERDICT for FOREX.md §3 requirement 2: PERSIST ON EVERY REFRESH.** The refresh
token is single-use. A persist-once design breaks on the second refresh, at whatever
hour the container happens to restart.

### Defect C1 — the exchange returned 201 and the token was silently not persisted

Saxo answers the token exchange with **`201 Created`**, not 200. The spike's first
version checked `if r.status_code != 200`, so a *successful* exchange took the error
branch, printed a scrubbed body, and exited **before** `save_tokens()`. Everything
looked like a failed call; in fact the credential had been issued and thrown away.

This is worth recording because it is precisely the failure mode §3 req. 2 guards
against, arriving from an unexpected direction: **a successful POST is not a
successful save.** Two consequences for M10b:

- The persist step needs a **positive assertion** — write, then read the file back and
  verify the values round-trip — not an inference from the HTTP status. Implemented in
  `spike/saxo.py:save_tokens()`, which also refuses an incomplete bundle.
- The success check must accept the **2xx range**. A vendor's exact success code is the
  same class of operational detail as its token lifetimes (see D2).

### Defect D2 — lifetimes are not constant

Observed `expires_in`: **1070, 1182, 1200**. Observed `refresh_token_expires_in`:
**3582, 3600**. These are not round numbers and they differ between responses.

**Nothing may derive an expiry from a hardcoded number.** Read `expires_in` from each
response and store an **absolute** expiry timestamp. Same lesson as the 201: vendor
operational values are read, never assumed. §3 requirement 1 already says this for
lifetimes; it should be widened to cover status codes too.

### D1 — the one-hour memory, and its operational cost

The access token lives ~20 minutes and the refresh token ~**1 hour**, rotating on every
use. Together these mean **the credential chain has a one-hour memory**:

| event | outcome |
|---|---|
| routine operation | fine — refresh every few minutes, each refresh resets the hour |
| deploy / container restart under ~1 h | survives, **if** the rotated refresh token was persisted (see C1) |
| server reboot, or any outage > 1 h | **refresh token is dead. Manual browser login required.** |

FOREX.md §3's framing — owner logs in once, copies the refresh token into `.env`, server
refreshes forever — is **false as written**. It holds only while the service never stops
for an hour.

**Is there a longer-lived credential?** Checked the vendor docs, not guessed. Saxo
documents Authorization Code, Authorization Code + PKCE, Implicit, and **Certificate
Based Authentication**. CBA is the only unattended server-to-server option and is
["only available to select partners upon request"](https://www.developer.saxo/openapi/learn/security).
There is no `client_credentials` grant and no service account. Saxo's own support
guidance confirms the standard flows all require a manual step and that an expired
refresh token forces a fresh login.

**Recommended handling:**

- Refresh on a **minutes** cadence — every ~5 min against a 20-minute token — with jitter
  and bounded retry, as §3 req. 3 says, but now with a concrete number.
- Treat refresh failure as urgent: forex goes dark within one token lifetime, not hours.
- The alert must **name re-authentication as the required owner action** and carry the
  authorize URL. A generic "forex paused" sends the owner looking for a data problem
  when the fix is a two-minute browser login.
- Persist the rotated refresh token inside the same transaction that uses it, and
  assert the write landed.
- Record this as a **standing operational cost of choosing Saxo**, visible before M10b
  is built: this provider cannot be made fully unattended at our account tier.

---

## 2. LIVE chart data on an unfunded account — VERIFIED, works

`GET /chart/v3/charts?AssetType=FxSpot&Uic=21&Horizon=60&Count=10` → **HTTP 200**, ten
candles, `DataVersion=29779589`, oldest `2026-08-20T22:00:00Z`, newest
`2026-08-21T07:00:00Z`. Row keys exactly as SIM:

```
['CloseAsk','CloseBid','HighAsk','HighBid','LowAsk','LowBid','OpenAsk','OpenBid','Time']
```

**No `NoAccess`. No market-data subscription needed.** No volume field — SIM's finding
holds on LIVE. No closed flag.

Two notes:

- `ChartInfo` and `DisplayAndFormat` came back as **empty objects** `{}` on v3, though
  the reference docs describe them as populated. `DelayedByMinutes` is therefore
  **absent from the chart response**. Precision must come from `/ref/v1/instruments/details`
  (item 4), not from the chart payload.
- `users/me` reports **`MarketDataViaOpenApiTermsAccepted: False`**, yet chart data and
  firm tradable quotes both flow. Worth watching: if Saxo ever enforces that flag,
  forex data stops. §12 should list it as a named pause cause.

**§1 assumption "live forex chart data is available without a paid subscription" — VERIFIED.**

---

## 3. The spread — VERIFIED, it **VARIES**. The most consequential result here.

1200 hourly candles per pair (2026-06-15 → 2026-08-21), `CloseAsk − CloseBid`, `Decimal`
throughout, pip from reference data.

| pair | n | min | median | mean | max | distinct values |
|---|---|---|---|---|---|---|
| EURUSD | 1200 | **1.0** | **1.1** | 1.32 | **18.3** | 46 |
| GBPUSD | 1200 | **1.1** | **1.8** | 2.83 | **41.3** | 95 |
| USDJPY | 1200 | **1.2** | **1.5** | 1.89 | **31.4** | 65 |

**VARIES, unambiguously.** The four OHLC fields carry *different* spread sets
(`all four fields carry the identical spread set: False`), which is the structural
opposite of SIM's constant offset added to a mid series.

### Independent control — `/trade/v1/infoprices`, EURUSD, 07:13 UTC

```
Bid 1.16965  Ask 1.16977  Mid 1.16971  ->  Ask-Bid = 0.00012 = 1.2 pips
DelayedByMinutes: 0   MarketState: Open   PriceSource: SBFX   PriceSourceType: Firm
PriceTypeBid/Ask: Tradable   BidSize 1,000,000  AskSize 5,000,000
```

A live firm quote at 1.2 pips against a chart median of 1.1 pips. The two sources agree,
so the chart spread is the real dealable spread, not a chart artifact. **This was the
question the control existed to answer, and it answers it cleanly.**

### The shape of the variation — it is a time-of-day effect, not noise

EURUSD median is 1.1 pips in **every hour from 00:00 to 19:00 UTC**. All the movement
sits in the last few hours of the session:

| hour UTC | EURUSD med / max | GBPUSD med / max | USDJPY med / max |
|---|---|---|---|
| 00–18 (typical) | 1.1 / ≤1.9 | 1.7–2.0 / ≤6.1 | 1.4–1.6 / ≤2.4 |
| 19:00 | 1.1 / **9.9** | 1.8 / **24.8** | 1.6 / **15.0** |
| 20:00 | 1.5 / **18.3** | **4.6** / **41.3** | 1.9 / **31.4** |
| **21:00 (rollover)** | **2.7** / 11.2 | **12.0** / 39.0 | **4.2** / 17.9 |
| 22:00 | 1.3 / 1.5 | 2.4 / 3.9 | 1.9 / 2.5 |

A full clean trading day (Thu 2026-08-20, EURUSD) sits at 1.0–1.2 pips for 21 of 24
hours, 1.3 at 20:00, **2.7 at 21:00**, back to 1.0 by 23:00.

### What this does to §7.3 and §7.4

**§7.3 measures.** The measured-spread series is real and per-hour-of-day, exactly as the
spec hoped. No configured fallback table, no "label it an assumption."

**§7.4's arithmetic was pessimistic by roughly half.** Restated with measured medians:

| pair | median | 18-pip cost | net from gross 1.5 | gross needed to net 1.5 | 40-pip cost | net from 1.5 |
|---|---|---|---|---|---|---|
| EURUSD | 1.1 pips | 0.061R | 1.439 | **1.561** | 0.028R | 1.473 |
| GBPUSD | 1.8 pips | 0.100R | 1.400 | **1.600** | 0.045R | 1.455 |
| USDJPY | 1.5 pips | 0.083R | 1.417 | **1.583** | 0.038R | 1.463 |

§7.4 assumed 2.0 pips → 0.11R on an 18-pip stop. Measured EURUSD is **0.061R**, near
half that. The qualitative conclusion is unchanged — a gross 1.5 RR still fails a 1.5
net gate, because any positive cost does — but the *size* of the required uplift is much
smaller. A setup needs gross **1.561** on EURUSD, not ~1.61.

**§5.3's rollover window is strongly justified by data.** At 21:00 UTC the cost of an
18-pip stop is 0.150R on EURUSD, **0.667R on GBPUSD**, 0.233R on USDJPY. A GBPUSD signal
at rollover loses two thirds of its risk budget to spread alone. The ±15-minute default
looks too narrow: elevated spreads are visible from **19:00 through 21:00 UTC**, three
hours, not thirty minutes. Recommend the owner widen the rollover exclusion or make it
spread-triggered rather than clock-triggered.

**§1 assumption "the live environment returns a real, varying spread" — VERIFIED.**

---

## 4. Reference data — VERIFIED, and it **contradicts §7.1**

`/ref/v1/instruments?Keywords=…&AssetTypes=FxSpot` resolved all three by keyword:

| symbol | Uic | description |
|---|---|---|
| EURUSD | **21** | Euro/US Dollar |
| GBPUSD | **31** | British Pound/US Dollar |
| USDJPY | **42** | US Dollar/Japanese Yen |

`/ref/v1/instruments/details?Uics=21,31,42&AssetTypes=FxSpot`:

| symbol | `Format.Decimals` | `Format.Format` | `OrderDecimals` | `TickSize` | `MinimumTradeSize` | `AmountDecimals` |
|---|---|---|---|---|---|---|
| EURUSD | **4** | AllowDecimalPips | 4 | **1e-05** | **1000.0** | 2 |
| GBPUSD | **4** | AllowDecimalPips | 4 | **1e-05** | **1000.0** | 2 |
| USDJPY | **2** | AllowDecimalPips | 2 | **0.001** | **1000.0** | 2 |

### The §7.1 contradiction — read this before writing the sizing module

§7.1 says *"0.01 for USDJPY, which quotes to three decimals"* and implies the others
quote to five. **The API does not say that.** It reports `Decimals` of **4** and **2**,
with `Format: AllowDecimalPips` meaning the venue displays one extra fractional-pip
digit on top. So:

```
pip = 10 ** -Format.Decimals
  EURUSD  Decimals=4 -> pip 0.0001   TickSize 1e-05 = 1/10 pip   (cross-check AGREES)
  GBPUSD  Decimals=4 -> pip 0.0001   TickSize 1e-05 = 1/10 pip   (cross-check AGREES)
  USDJPY  Decimals=2 -> pip 0.01     TickSize 0.001 = 1/10 pip   (cross-check AGREES)
```

The *pip values* §7.1 states are right. Its *stated derivation* is wrong, and the wrong
derivation is the dangerous part: a reasonable implementer reading "quotes to 5
decimals, pip is 0.0001" writes `pip = 10**-(decimals-1)`. Against the real `Decimals=4`
that yields **0.001 — every spread and every stop distance wrong by 10×**, with
plausible-looking output and no error. This spike made exactly that mistake and caught
it only because the reference data disagreed with the spec.

**Recommend §7.1 be rewritten to state `pip = 10**-Format.Decimals`, cross-checked
against `TickSize × 10`, with the `AllowDecimalPips` flag explained.**

### Other findings

- **`MinimumTradeSize = 1000.0` on all three — §7.2's 1000-unit assumption VERIFIED.**
  Its whole arithmetic (974 units at an 18-pip stop, `BELOW_MIN_TICKET` at wider stops,
  ~€205 minimum viable capital) stands on measured ground.
- `LotSize: null`, `LotSizeType: "NotUsed"` — no lot rounding, units are free above the
  minimum.
- **No `MarginRates`, no `MarginTiers`, no `TradingSessions`** in the details response.
  Full key list is in `raw/4b_instrument_details.json`. **§7.5's "read the venue's actual
  max leverage where available" is not satisfiable from this endpoint** — it must come
  from config with the ESMA 30:1 cap as the documented assumption, or from a different
  endpoint not found here.

**§1 assumption "reference data returns MinimumTradeSize and decimal precision" — VERIFIED.**

---

## 5. Economic calendar — **FAILED.** Use the YAML.

`GET /root/v1/features/availability` → 200:

```json
[{"Feature":"Calendar","Available":true},{"Feature":"Chart","Available":true},
 {"Feature":"GainersLosers","Available":true},{"Feature":"News","Available":true}]
```

So Saxo reports a Calendar feature as **available** — but exposes no reachable path.
The public reference docs list no calendar service group at all (groups are: hist, atr,
chart, cm, cr, cs, ca, dm, ens, mkt, partnerintegration, port, ref, reg, root, trade,
vas). Every candidate path returned **404**:

```
404  /mkt/v1/eventcalendar        404  /mkt/v1/economiccalendar
404  /vas/v1/eventcalendar        404  /ref/v1/eventcalendar
404  /cal/v1/events
```

These five paths are **guesses**, and are reported as guesses. Their 404s prove those
specific URLs are wrong; they do not prove no endpoint exists. What is established is
that **no documented, discoverable calendar endpoint is reachable on this account.**

**Consequence for §8:** the hand-maintained YAML is not a backstop, it is the **primary
and only** source. §8's "primary: Saxo's own calendar service, verify this early" should
be struck. The rest of §8 — empty response treated as failure, never as "no events" —
becomes more important, not less, because there is no second source to disagree with.

**§1 assumption "an economic calendar is reachable through the same API" — FALSE.**

---

## 6. Market-closed behaviour — VERIFIED, plus a reproducible anomaly

### The clean result

`Mode=From`, `Time=2026-08-14T18:00:00Z`, `Count=60`:

```
60 candles, 2026-08-14T18:00:00Z .. 2026-08-19T03:00:00Z
GAP: 2026-08-14T20:00:00Z -> 2026-08-16T19:00:00Z   (1 day, 23:00:00)
```

**Closed hours are simply ABSENT.** Not an error, not an empty array, not a repeated
Friday candle, not zero-filled. The last Friday bar is stamped `20:00Z` (covering
20:00–21:00), so the week closes at **21:00 UTC** as §5.2 expects.

`Mode=From` with a `Time` inside the closed window rolls forward to the next open bar
rather than erroring — requesting 24h from Sat 00:00Z returned 24 candles beginning
`2026-08-16T21:00:00Z`.

**For §5.1 this is the good outcome:** a closed market is distinguishable from stale data
by the *absence* of bars in a known window, and `market_hours()` can be derived from
candle availability exactly as §5.2 asks. The staleness rule must still be checked after
market hours, or every Monday-morning snapshot reports DEGRADED.

### The anomaly — the series is not stable across queries

Bars exist at `2026-08-16T19:00:00Z` and `20:00:00Z` with real, wide-spread OHLC
(19:00 close spread 1.9 pips, 20:00 **6.7 pips** — a thin reopen). They appear when the
request is anchored at Fri 18:00Z with `Count ≥ 50`. They **vanish** when anchored later
or with a smaller Count. Reproducible, not a transient:

| request | Sun 19:00 & 20:00 present? |
|---|---|
| `From Fri 18:00Z, Count=30` | **0 / 2** |
| `From Fri 18:00Z, Count=50 / 59 / 60 / 61 / 120` | **2 / 2** |
| `From Sat 00:00Z, Count=24 / 48` | 0 / 2 |
| `From Sun 12:00Z, Count=24` | 0 / 2 |
| `From Sun 19:00Z, Count=12` | 0 / 2 |
| `UpTo Sun 23:00Z, Count=12` | 0 / 2 |

**The same hour of the same instrument is present or absent depending on the request's
anchor and `Count`.** I could not determine the mechanism and am not going to guess one.

**Two consequences the owner should weigh before §4.4 is built:**

- **§4.4 says "if a tail exceeds the documented maximum, page it." Paging is unsafe
  under this behaviour** — two pages with different anchors can disagree about whether a
  given hour exists, producing a tail with a hole or a duplicate depending on where the
  page boundary lands. Prefer a single request (1200 bars covers every tail in §4.4)
  and treat paging as an open problem.
- The exact week-open boundary is ambiguous: **19:00Z or 21:00Z on Sunday** depending on
  how you ask. §5.2's instruction to derive sessions from candle availability needs to
  specify *which query* defines availability, or it will produce different answers on
  different days.

---

## 7. Limits — VERIFIED

- **`Count` max is 1200**, matching the docs. `Count=1201` returns **HTTP 200 with 1200
  candles — a silent clamp**, no error, no warning. Anything that assumes it got what it
  asked for will quietly analyse a shorter tail. `Count=120` on a range with only 112
  bars available returned 112, so `Count` is an upper bound in both directions.
- **Rate-limit headers are present on every chart response:**

```
x-ratelimit-appday-limit: 10000000      x-ratelimit-appday-remaining: 9999937
x-ratelimit-chartminute-limit: 120      x-ratelimit-chartminute-remaining: 112
x-ratelimit-chartminute-reset: 24
```

Per-service-group, per-minute: **120 requests**. The whole spike — 89 calls including a
47-poll burst — consumed 63 of 10,000,000 daily. Rate limits are a non-issue at
Sentinel's cadence, but the `x-ratelimit-*-remaining` headers should be logged so a
runaway loop is visible before it 429s.

---

## 8. News — **FAILED for our purposes**

`features/availability` reports `News: Available=true`. Every candidate path 404s except
one:

```
404  /news/v1/newsitems     404  /mkt/v1/news     404  /vas/v1/news
200  /trade/v1/messages  ->  []
```

`/trade/v1/messages` is **broker-to-client messaging, not market news**, and returned an
empty array. Same caveat as item 5: the 404s disprove those specific guessed URLs, not
the existence of some endpoint.

**Consequence for §10:** no change needed — §10 already names central-bank RSS (Fed, ECB,
BoE, BoJ) as primary and Saxo news as a "secondary if it proves useful." It did not prove
useful. Strike the secondary.

---

## A2 — candle-close grace margin (new measurement, feeds §4.1)

§4.1 needs a `grace` value and had none. Measured two ways.

**First attempt (`step4_grace.py`, Horizon=60, 14 samples over 7 min) produced nothing
usable** — every sample caught the newest bar mid-formation, 2423–2838 s *before* its
nominal close. The run never crossed an hour boundary, so it never observed the quantity
`grace` guards. Reported rather than papered over.

**Second attempt (`step5_publish_lag.py`, Horizon=1, 47 polls at 5 s over 4 min)** — a
1-minute bar rolls every minute, so the same publish latency is observable many times:

| bar closed | nominal close | next bar first seen | lag |
|---|---|---|---|
| 07:20:00Z | 07:21:00Z | 07:21:04Z | ≤ 4 s |
| 07:21:00Z | 07:22:00Z | 07:22:01Z | ≤ 1 s |
| 07:22:00Z | 07:23:00Z | 07:23:03Z | ≤ 3 s |
| 07:23:00Z | 07:24:00Z | 07:24:00Z | ≤ 0 s |

**Publish lag: 0–4 s** (upper bounds; 5-second poll resolution makes each at most 5 s
pessimistic).

**Mutability — the sharper question.** 9 bars tracked, 42 value changes observed. 38 were
changes to the bar while it was still the newest, i.e. normal formation. **4 were changes
to a bar that was no longer newest — but all four occurred 0–4 s past nominal close, at
the roll itself, and none more than 5 s past.** So a bar takes its final tick as it
closes and is stable thereafter. `step5`'s printed conclusion ("keeps mutating") is
stronger than the data supports; the accurate statement is **a bar finalises within ~4 s
of its nominal close and does not change afterwards.**

**RECOMMENDED grace: 30 seconds.** Derivation, not convention: observed max lag 4 s,
plus 5 s poll pessimism = 9 s worst observed. 30 s is ~3× that with headroom for the
fact that all samples come from one 4-minute window on one instrument during liquid
London hours. Two caveats the owner should weigh:

- The measurement is on **Horizon=1**. Publish latency should be horizon-independent
  (it is feed latency), but this was not proven on Horizon=60.
- 30 s is negligible against a 60-minute bar, so the cost of being generous is nil,
  while the cost of being tight is a poisoned indicator. Round up, not down.

§4.1's rule — `now >= T + H + grace`, drop the newest bar otherwise — is **sound and
sufficient**, given the bar is stable after close. Keep it.

---

## §1 ledger — what is now settled

### ASSUMED → **VERIFIED**

| §1 assumption | result |
|---|---|
| Live environment returns a **real, varying spread** | **VERIFIED.** 46–95 distinct values per pair; independently confirmed by a firm infoprices quote |
| Live forex chart data available **without a paid subscription** | **VERIFIED.** HTTP 200 on an unfunded account, no `NoAccess` |
| Reference data returns **`MinimumTradeSize`** and decimal precision | **VERIFIED.** 1000.0 on all three; `Format.Decimals` + `TickSize` present |
| Unfunded live account keeps API access | **VERIFIED today.** Inherently a standing risk, not a one-time proof |

### ASSUMED → **FALSE**

| §1 assumption | result | what must change |
|---|---|---|
| **An economic calendar is reachable through the same API** | **FALSE.** Feature flag says available; no reachable path, 5/5 candidates 404 | **§8:** strike "primary: Saxo calendar." The YAML is the only source. **§2** table row unaffected |

### Additional spec defects found — none of these were in §1

| # | finding | section to change |
|---|---|---|
| D-a | `Format.Decimals` is **4 and 2**, not 5 and 3. Pip is `10**-Decimals` | **§7.1** — rewrite the derivation; the stated pip values are right, the reasoning is wrong and fails by 10× |
| D-b | Refresh token lives **~1 h and rotates**. Outage > 1 h ⇒ manual browser login. No longer-lived credential exists at our tier | **§3** — "log in once, forever" is false; add re-auth alerting, persist-every-refresh, and record the operational cost |
| D-c | Token exchange returns **201**, and lifetimes vary (1070/1182/1200) | **§3** — accept 2xx; read every operational value, never hardcode; assert the persist landed |
| D-d | Chart series **not stable across queries** — same hour present or absent by anchor and `Count` | **§4.4** paging is unsafe; **§5.2** week-open ambiguous (19:00Z vs 21:00Z Sunday) |
| D-e | `Count` over-request **silently clamps** to 1200 | **§4.4** — assert the returned count, never assume |
| D-f | No `MarginRates`/`MarginTiers` in instrument details | **§7.5** — "read the venue's actual figure" is not satisfiable; config + documented ESMA assumption |
| D-g | Elevated spreads span **19:00–21:00 UTC**, not ±15 min. GBPUSD at 21:00 costs **0.667R** on an 18-pip stop | **§5.3** — widen the rollover window or make it spread-triggered |
| D-h | Chart v3 returns **empty `ChartInfo`/`DisplayAndFormat`**; no `DelayedByMinutes` | **§4.2** — precision must come from reference data only |
| D-i | `MarketDataViaOpenApiTermsAccepted: False` while data flows | **§12** — add as a named potential pause cause |
| D-j | Real tail lengths are **321/321/321/101** (config.yaml), not the 201/201/201/101 §4.4 states | **§4.4** — correct the numbers before quoting them |
| D-k | **4h and 1d bars are anchored to 17:00 America/New_York and shift with US DST** — not UTC. The 1d `Time` stamp is a label, not the bar's start | **§6.1** — this partly decides the open question; **§4.4** — a forex 4h bar never aligns with a crypto 4h bar |

### Unchanged from SIM, re-confirmed on LIVE

No volume field of any kind. No candle-closed flag. Bid and ask on all four OHLC fields.
UTC ISO-8601 timestamps on the hour.

---

## E3 — does every timeframe fit in one request? **Yes. Forbid paging.**

The premise needs one correction first: **the real tails are 321/321/321/101**, not
201/201/201/101 as §4.4 states. `config.yaml` requests 321 for the chart timeframes so
that 320 closed candles yield 121 valid EMA200 points across a 120-candle chart window;
only 1d keeps the 101. §4.4's numbers are already stale against the running config.

Confirmed empirically, not by arithmetic — every request returned exactly what it asked
for, in one call:

| timeframe | Saxo `Horizon` | asked | returned | span covered |
|---|---|---|---|---|
| 15m | 15 | 321 | **321** | 2026-08-17T23:30Z → 2026-08-21T07:30Z |
| 1h | 60 | 321 | **321** | 2026-08-04T03:00Z → 2026-08-21T07:00Z |
| 4h | 240 | 321 | **321** | 2026-06-10T13:00Z → 2026-08-21T05:00Z |
| 1d | 1440 | 101 | **101** | 2026-04-03T00:00Z → 2026-08-21T00:00Z |
| 1m (tracker fill) | 1 | 5 | **5** | 2026-08-21T07:30Z → 07:34Z |

Largest requirement is **321 against a 1200 ceiling — 27% of one request.** Every
`Horizon` we need (1, 15, 60, 240, 1440) is in Saxo's allowed enum. **No timeframe comes
close to needing a second request, so §4.4 can forbid paging outright** rather than
trying to make it safe — which is the right call given D-d, since a page boundary is
exactly where the anchor-dependent series instability would bite.

Two guards remain worth keeping: assert the returned count equals the requested count
(D-e: over-requesting clamps silently), and treat a short return as a degraded read
rather than a shorter tail.

## D-k — bar alignment is 17:00 New York, and it moves with DST

Found while confirming E3. Saxo's 4h grid is **not** on the UTC quarter-day, and the
daily bar does **not** start at 00:00 UTC.

The 1d bar stamped `2026-08-17T00:00:00Z` reports `Open 1.15625, High 1.16137,
Low 1.15578, Close 1.15789`. Reconstructing from hourly bars:

| window | open | high | low | close |
|---|---|---|---|---|
| Mon 00:00–24:00 **UTC** | 1.15731 | 1.16137 | 1.15704 | 1.15822 |
| **Sun 21:00 → Mon 21:00 UTC** | 1.15605 | 1.16137 | 1.15605 | **1.15789** ✓ |
| the 1d bar itself | **1.15625** | 1.16137 | **1.15578** | **1.15789** |

The bar's close matches the 21:00-anchored window exactly. Its open (1.15625) and low
(1.15578) match the `2026-08-16T19:00:00Z` hourly bar's `OpenBid`/`LowBid` **exactly** —
the early-Sunday bars from D-d. So the daily bar runs from the week's actual open through
**Monday 21:00 UTC**, and its `00:00:00Z` timestamp is a **label carrying the date, not
the bar's start time.**

The anchor tracks US daylight saving. Same instrument, two seasons:

```
JANUARY (US EST, UTC-5):  4h stamps  02:00 06:00 10:00 14:00 18:00 22:00   -> anchor 22:00Z
AUGUST  (US EDT, UTC-4):  4h stamps  01:00 05:00 09:00 13:00 17:00 21:00   -> anchor 21:00Z
```

**22:00Z in winter and 21:00Z in summer are both 17:00 America/New_York.** Saxo's native
bars are on the FX convention, and the boundary moves by an hour twice a year — exactly
the DST hazard §5.2 warns about, now confirmed to affect candle *alignment* and not just
session hours.

**Consequences for §6.1, which is an open decision:**

- Saxo's native `Horizon=1440` and `Horizon=240` bars **are** the 17:00 New York
  convention. Choosing that option costs nothing — just use them.
- **Choosing UTC means computing daily and 4h bars ourselves by aggregating hourly
  candles.** §6.1's recommendation of UTC "for determinism" is still defensible, but it
  is not free, and the spec should say so.
- Either way, a forex 4h bar **never** aligns with a crypto 4h bar (01/05/09… vs
  00/04/08…), and the offset changes at DST. Anything comparing the two markets on a 4h
  grid is comparing different windows.
- 15m, 1h and 1m bars are unaffected — they sit on the UTC grid regardless.

## Reproducing

```bash
.venv/bin/python spike/step1_oauth.py          # interactive; paste the redirect URL
.venv/bin/python spike/step2_probe.py          # items 2,4,5,6,7,8 + infoprices
.venv/bin/python spike/step3_spread.py         # item 3
.venv/bin/python spike/step5_publish_lag.py    # A2 grace margin
```

Tokens cache outside the repo (session scratchpad, mode 0600) and are never committable.
`spike/` is untracked and outside `make check`'s scope (`ruff`/`mypy` run on
`sentinel tests alembic`; `testpaths = ["tests"]`), so none of this can affect the gate
or the running crypto system.
