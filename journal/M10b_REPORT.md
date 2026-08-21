# M10b-1 — the forex data spine and sizing core

**Date:** 2026-08-21. **Status:** complete, `make check` green, **not deployed.**

M10b was split in two during planning (owner decision, 2026-08-21). This session is
**M10b-1** — build-order steps 1–6 of `docs/specs/FOREX.md` §14, plus migration `0011`
and the new `sentinel/fx/` package. Features, charts and wiring are **M10b-2**; the
forex card, publishing and tracking are **M10c**. §12 below is explicit about what did
not ship.

Everything here is behind `markets.forex.enabled: false`. The orchestrator still
builds only the Binance adapter and has no forex path at all, so **deploying this is a
no-op for the running crypto system** — and that is asserted rather than asserted-ish:
`test_a_crypto_cycle_never_touches_a_saxo_adapter_or_credential` makes constructing a
Saxo adapter or a Saxo credential an error for the duration of a full crypto cycle,
and the cycle completes.

**Read §2 and §3 first.** One of them is a pre-existing defect that would have broken
the next deploy, and the other is an open question about the deployed image that only
the owner can settle.

---

## 1. The binding constraint held

```
$ git diff --stat sentinel/risk/ sentinel/analyst/prompts/
(no output)
```

Risk-engine branch coverage unchanged at **100%** — 604 statements, 152 branches,
identical to the figures in journal/M10a_REPORT.md. All 23 golden fixture files are
**byte-identical** by sha256, checked after every step rather than at the end, and
`tests/golden/`'s thirty assertions pass unchanged.

---

## 2. `make check` found a live packaging defect that predates this milestone

**The image could not boot.** `make check-image` failed on `import sentinel.main`:

```
File "/app/sentinel/llm/client.py", line 33, in <module>
    import httpx
ModuleNotFoundError: No module named 'httpx'
```

**It is not mine.** Verified by stashing this branch entirely and building the image at
the pristine `4c90819`: identical failure. **The next deploy would have failed at boot.**

**Cause.** Six modules `import httpx` directly — `ingestion/http.py`, `llm/client.py`,
`core/wiring.py` and three tools — and it was never in `pyproject.toml`. It arrived
transitively through `anthropic`, which has since moved to `httpx2`. A freshly resolved
image therefore has no `httpx` at all, while `.venv` — resolved months ago — still does.

**Fix:** `httpx>=0.27` declared as the direct dependency it has always been.

This is precisely what journal/M7_REPORT.md §6 added the in-image *import* for. The
wheel built cleanly, `pip install` succeeded, and only actually importing the app
caught it. The gate earned its cost today.

---

## 3. RESOLVED — three versions of the Anthropic SDK, in three places

Found while fixing §2. Before the fix, `pyproject.toml` said `anthropic>=0.122` and so
every environment resolved something different:

| | version | evidence behind it |
|---|---|---|
| the **server** | **0.125.0** (verified on the box) | **37 completed live cycles** |
| the local test venv | 0.122.0 | the suite, against a mocked transport |
| a freshly built image | **1.0.0** | none — a major bump, arriving at the next deploy |

So the suite validated a version production does not run, and the next rebuild would
have shipped a **third** version to the one dependency that talks to the model. The
image *imports* cleanly under 1.0.0, which is all `check-image` proves; it says nothing
about whether `messages.create`'s parameters or error types still behave as
`sentinel/llm/client.py` expects. A live cycle would have been the first to find out,
at ~$0.32 a call, unattended.

**Owner ruling, 2026-08-21 — pin both to production's versions:**

```
anthropic==0.125.0
httpx==0.28.1
```

*"0.125.0 has 37 completed live cycles behind it. That is stronger evidence than any
test suite, and it is the only version with evidence at all. Upgrading to 1.0.0 is its
own milestone with its own testing, not a silent side effect of the next deploy during
a live measurement window."*

This follows the convention `matplotlib==3.11.1` and `mplfinance==0.12.10b0` already
set — pinned exactly because a silent upstream change would alter behaviour with no
error — rather than inventing one. `httpx` **stays explicitly declared** even though
0.125.0 brings it transitively: six modules import it directly, and an undeclared
direct dependency is a latent break whichever way the transitive chain moves next.

**The local venv was reinstalled at 0.125.0 and the full suite re-run.** Tests
validating a version production does not run is the gap that let this through in the
first place, so it was closed rather than noted.

| | before | after |
|---|---|---|
| suite | 1767 passed, 60 skipped, 1 warning | **1767 passed, 60 skipped, 1 warning** |
| `mypy --strict` | clean, 264 files | **clean, 264 files** |
| golden fixtures | 23 byte-identical | **23 byte-identical** |

**No behavioural difference between 0.122.0 and 0.125.0 was found.** Two caveats on how
much that is worth. The suite drives a **scripted transport** (`tests/anthropic_double.py`),
so it exercises our client against the SDK's types and call surface, not its network
behaviour. What *is* strong evidence is `mypy --strict` passing against the real
installed 0.125.0: every symbol `sentinel/llm/client.py`, `screener.py` and
`anthropic_fable.py` import — `AsyncAnthropic`, `omit`, `Message`, `MessageParam`,
`TextBlock`, `TextBlockParam`, `ImageBlockParam`, `OutputConfigParam`,
`APITimeoutError`, `APIStatusError`, `APIConnectionError` — still exists with a
compatible signature.

The image now resolves to production's versions exactly:
`image anthropic 0.125.0 | httpx 0.28.1`.

### And the part worth remembering

**Production would have kept running fine.** Nothing was broken on the server: it holds
an image built when the transitive chain still supplied httpx. **The next image rebuild
would have failed at boot** — on the first `import sentinel.main`, before a single cycle.

The wheel built cleanly throughout. That is precisely why `check-wheel` alone was never
sufficient, and why journal/M7_REPORT.md §6 added an **in-image import** to the gate:
`pip install` never imports the package, so a dependency present in `.venv` and absent
from the image builds perfectly and dies at startup. That check is the only thing in
this repository that caught it.

---

## 3a. `make check-deps` — and the two more defects it found immediately

§2 and §3 are one defect wearing two hats, and the shape of it is what makes it worth
guarding: **nothing in this repository could have raised it.** Tests pass, `mypy`
passes and the wheel builds, because all three run somewhere `httpx` happens to be
installed. It took an unrelated upstream change to surface it, and it surfaced one
rebuild later than anybody would want.

So the **declaration** is now checked rather than the installation
(`sentinel/tools/check_deps.py`, wired into `check-fast` and therefore into
`make check`). It parses the AST of every file under `sentinel/`, resolves each
top-level import to the distribution that provides it, and asserts that distribution is
in `[project].dependencies`. It reads the source tree, not a resolved environment, so it
fails on the commit that introduces the import rather than on some later day when a
third party reorganises its own requirements.

Extras are stripped (`sqlalchemy[asyncio]` is a dependency on `sqlalchemy`), names are
PEP 503-normalised (`PyYAML` and `pyyaml` are one), imports inside `if TYPE_CHECKING`
count — `ingestion/models.py` imports pandas exactly that way — and **optional-dependency
groups deliberately do not count**: a runtime import satisfied only by a dev extra is
the same latent break.

**On its first run it found two more instances of the same defect:**

| Module | Imported directly by | Was arriving via |
|---|---|---|
| `numpy` | `sentinel/features/indicators.py` (`import numpy as np`) | pandas / matplotlib |
| `annotated_types` | `sentinel/bot/pulse.py` (`from annotated_types import MaxLen`) | pydantic |

Both now declared. Neither is a *new* dependency — nothing extra is installed — but each
was one upstream reorganisation away from being the next `httpx`. They are declared with
`>=`, not pinned: `anthropic` and `httpx` were pinned to versions with live production
evidence behind them, and there is no such evidence to pin these to.

**One thing this surfaces and does not settle.** `matplotlib` and `mplfinance` are
pinned exactly so that stored charts stay byte-reproducible. `numpy` sits underneath
both, and underneath every indicator value the feature engine computes — and it is
unpinned. That risk is not new and this milestone does not change it, but it is now
visible in `pyproject.toml` where it was previously invisible. Worth its own decision.

Seven tests cover the checker, and the important ones are the non-vacuity ones — a
checker never seen to fail is indistinguishable from one that cannot fail, which is this
project's own named failure mode. `test_the_check_catches_the_defect_it_was_written_for`
reconstructs the M10b-1 defect exactly: a package importing `httpx` against a pyproject
that does not declare it.

---

## 3b. `alembic upgrade head` exited 0 having done nothing

Found by the owner while verifying the migration. With a stale local image, alembic
reported `0010_market_dimension` as head, **skipped `0011` entirely, and exited 0.**

Nothing said so. The only signal was the **absence** of a line — no
`Running upgrade 0010_market_dimension -> 0011_forex_spine` in the output. An exit code
was checked, a success was inferred, and the migration had not run.

**This is the third instance of one pattern in a single day:**

| | looked like success | was |
|---|---|---|
| `make check-wheel` (§2) | wheel built cleanly | app could not import |
| the token exchange (§5 D, from the spike) | HTTP 2xx, "saved" printed | nothing persisted |
| `alembic upgrade head` (here) | exit 0 | nothing migrated |

In all three, the positive signal was real and simply did not mean what it was taken to
mean. This is journal/M8_REPORT.md's "silence-as-success" arriving from a third
direction, and the fix is the same one each time: **assert the thing you actually want,
not a proxy for it.** The wheel check became an in-image *import*; the token persist
became a read-back *comparison*; and this needs the same.

**Action for the M10a §8 verification recipe (docs, not code):** it currently says
`alembic upgrade head` and moves on. It must say: **assert alembic reported the expected
upgrade — do not merely check that it exited 0.** Concretely, either grep the output for
`Running upgrade <previous> -> <target>`, or read `alembic_version` afterwards and assert
it equals the expected revision.

`ops/verify-migration.sh` already does the second of those —
`[[ "$(revision_now)" == "$TARGET_REVISION" ]]` after every upgrade and downgrade, three
times — which is why the script's run in §10 could not have been fooled the same way. It
is the **hand-run** recipe in journal/M10a_REPORT.md §8 that has the gap, and that is the
one somebody follows at a terminal on the server.

---

## 4. Five more spec defects, all found by reading FOREX.md against the code

The spike checked the spec against the live API. These came from checking it against
this repository, which is a different exercise. All five are recorded dated in
`docs/specs/FOREX.md`'s corrections log; two changed shipped behaviour.

### #12 — `TradePlan` cannot carry a forex plan (§7, §11)

It lives in the frozen `sentinel/risk/` and is crypto-shaped: `notional_usdt`,
`suggested_leverage`, `liq_distance_pct`, `liq_buffer_ok`, and a `PlanCosts` carrying a
perpetual-futures funding rate. `bot/cards.py` reads all four directly. §7.6 forbids
faking the liquidation buffer and the package may not be extended, so §7/§11's implicit
assumption is false.

**Owner ruling:** M10b ends at a **sizing result**, in a new `sentinel/fx/` package, and
the rule is stronger than "leave it null" — *if a concept does not exist in this market,
there is no field to put it in*. Asserted, not merely intended:

```python
assert not [name for name in fields if "liq" in name or "funding" in name]
```

on both `ForexSizing` and `ForexCosts`. Swap/rollover *is* a real forex concept and is
named `rollover_*`, never `funding_*`.

### #13 — "omit, never zero" had nowhere to land (§2.1)

`Candle.volume` was a required Pydantic field and `ohlcv_candles.volume` was NOT NULL,
so a forex candle could not express the absence §2.1 demands. Both changed together
(owner sign-off for the contract change and the schema change).

The danger the permission creates is the reverse one, and it is policed in code in both
directions: **a crypto candle with a null volume is an error, and a forex candle with a
non-null volume is an error** — including a zero, which is the value somebody would
reach for. Enforced on `OHLCVSeries` at construction *and* again at the storage seam,
where a forex series meeting a crypto-scoped repository is caught. Eight tests.

### #14 — §5.1's premise is inverted

§5.1 says crypto's staleness rule "would fire on every weekend snapshot" and must
therefore be preceded by a market-hours check. It would not:
`sentinel/ingestion/staleness.py` compares `OHLCVSeries.fetched_at`, which is always
~now for a fresh fetch, **not** the newest candle's `open_time`.

The real hazard is the opposite and worse — a weekend snapshot whose newest bar is fifty
hours old reads as perfectly **fresh**. §5.1's conclusion is right and its reasoning is
backwards: forex needs a **candle-recency** check crypto never needed, and *that* is the
check which must be skipped while the market is closed. Both halves are in
`sentinel/fx/hours.py`.

### #15 — the spread gate's baseline was self-defeating (§5.3)

§5.3 gated on "a configured multiple of that instrument's median **for that
hour-of-day**". At 21:00 UTC GBPUSD's hour-of-day median *is* 12.0 pips, so a 12-pip
spread at 21:00 is "normal for that hour" and passes — while costing 0.667R of an
18-pip stop. The per-hour baseline normalises away exactly the widening the gate exists
to catch, and the gate never fires at the one hour it was written for.

**Owner correction:** the **cost model** keeps the per-hour median; the **gate** uses the
instrument's **global** median. `test_the_per_hour_baseline_would_never_fire_at_the_hour_it_is_for`
computes §5.3-as-written alongside what ships, so the defect stays visible.

Threshold **3.0 × the global median** — EURUSD 1.1 → 3.3 pips, GBPUSD 1.8 → 5.4, which
its 21:00 median of 12.0 fails. **3.0 is a starting guess to be calibrated from DRY_RUN
data, not a derived figure**, and every firing logs instrument, hour, spread and
threshold because how often it fires is itself a measurement.

### #16 — net RR was computed the wrong way round (§7.4)

§7.4 computed `net = gross − cost/risk`. That is not what a round-trip spread does. §7.5
settles that levels come from the **bid** series and execution is asymmetric — a long
enters at ask and exits at bid — so one spread `s` makes the loss `risk + s` **and** the
gain `reward − s`. It lands on both sides at once. `sentinel/risk/costs.py` has computed
crypto's net RR that way since M4; forex was the odd one out.

On an 18-pip stop from a gross 1.5:

| Pair | Spread | §7.4 said | **Actual** | §7.4 said | **Actual** |
|---|---|---|---|---|---|
| | | net | net | gross needed | gross needed |
| EURUSD | 1.1 | 1.439 | **1.356** | 1.561 | **1.653** |
| USDJPY | 1.5 | 1.417 | **1.308** | 1.583 | **1.708** |
| GBPUSD | 1.8 | 1.400 | **1.273** | 1.600 | **1.750** |
| **GBPUSD 21:00** | **12.0** | 0.833 | **0.500** | 2.167 | **3.167** |

§7.4's qualitative conclusion survives — any positive cost sinks a gross 1.5 — but the
required uplift is about **two and a half times** what it claimed, because the cost is
charged `1 + target` times rather than once. **Owner ruling: ship the corrected model.**

### Smaller corrections

- The adapter is `sentinel/ingestion/adapters/forex_saxo.py`, beside the Binance one.
- Instruments cache in a new **`forex_instruments`** table, not `instrument_meta` —
  which is a tick size, a quantity step, a minimum *notional* and a contract size, and
  has no home for a Uic, a `Format.Decimals` or a pip. Same defect class as #12.
- The spread profile is **computed** from the 1h tail rather than stored. §7 in full.

---

## 5. The five named hazards

**A — the pip.** `pip = 10 ** -Format.Decimals`, asserted against `TickSize × 10`, and
`ForexInstrument` **cannot be constructed** with a pip that fails either half. The
load-bearing test is not that the right derivation works but that the wrong one is
rejected: `test_the_wrong_derivation_fails_loudly_on_every_pair` feeds
`10 ** -(decimals - 1)` — the natural reading of v1's "5 decimals" — and asserts it
raises for all three pairs, through the function *and* through the model. Precision is
read from reference data only; a test populates `ChartInfo`/`DisplayAndFormat` with a
*different* precision and asserts the pip does not move.

**B — the forming candle.** `now >= T + H + 30s`, tested at one tick before (dropped),
exactly at (kept) and one tick after (kept). Grace derived, not chosen: 0–4 s observed
lag plus 5 s poll pessimism is 9 s worst, 30 s is ~3× that, and being generous costs
nothing against a 60-minute bar.

**C — paging.** Forbidden. One request per timeframe, **no `Mode` and no `Time` ever
sent**, so the series cannot vary with an anchor the way D-d showed it can — asserted by
a test that counts chart requests and inspects their parameters. `len(returned) ==
requested` or `DegradedRead`; an over-request above the ceiling is refused *before* the
call, because Saxo clamps silently.

**D — token persistence.** Persist on every refresh, then **read back and compare**.
Two doubles model the failure: a store that keeps the *previous* value (a failed UPDATE
— the realistic shape, and invisible to anything checking HTTP status) and one that
reads back empty. Any 2xx is success (Saxo returns **201**). Every lifetime is read from
the response — 1070/1182/1200 and 3582/3600 are all covered — and stored as an absolute
instant. A missing `refresh_token_expires_in` means **unknown**, never "does not
expire". No token value can reach a log line: a test greps captured structlog output for
every secret and fails if any appears. And `bootstrap` **never overwrites a stored
credential**, because letting a redeploy rewind the chain to the spent `.env` token
would force the very login it exists to avoid.

**E — missing data looks missing.** §4's volume invariant, in both directions. The
forex prompt half of §2.1 belongs to M10b-2 and is not claimed here.

---

## 6. What the €200 account can actually trade

§15 asks for the `BELOW_MIN_TICKET` rate at €200. There is no production data yet, so
this is the analytic answer — which is sharper than a rate, because it is a hard edge.

At €200 and 0.75% risk (€1.50), with `MinimumTradeSize = 1000` on all three pairs:

| Pair | Widest stop that sizes at all | Min capital @ 18 pips | Min capital @ 40 pips |
|---|---|---|---|
| EURUSD | **17.5 pips** | €205.30 | €456.23 |
| GBPUSD | **17.5 pips** | €205.30 | €456.23 |
| USDJPY | **25.5 pips** | €141.18 | €313.73 |

**The spec's own example fails.** §7.2's 18-pip EURUSD stop sizes to 974 units and is
rejected; the account tops out at 17.5 pips. USDJPY reaches further only because a yen
pip buys far more units per euro of risk.

And §7.2's warning that "tight stops are where spread hurts most" is exactly right, at
the widest stop each pair can still take:

| Pair | Stop | Units | Spread | Cost | Gross needed to net 1.5 |
|---|---|---|---|---|---|
| EURUSD | 17.5p | 1002 | 1.1p | €0.0943 (6.29% of risk) | **1.66** |
| GBPUSD | 17.5p | 1002 | 1.8p | €0.1543 (10.29% of risk) | **1.76** |
| USDJPY | 25.5p | 1000 | 1.5p | €0.0882 (5.88% of risk) | **1.65** |

So at €200 the tradeable band is narrow, and every setup inside it needs a gross RR
around 1.65–1.76 to clear a 1.5 net gate. **That is the measurement, and it argues that
€200 is below the viable size for this market** — which is worth knowing before any
money is at stake, and is exactly what DRY_RUN is for.

---

## 7. Deliberate deviations, and why

**The spread profile is computed, not stored (§7.3).** From the 1h tail the adapter
already fetched. No extra request, no extra table, and the statistic stays a pure
function of data the cycle already holds.

**The forex 1h tail is 1200 bars, not 321** (owner correction). 321 is ~13 days, leaving
~10 samples per hour-of-day bucket after weekends, and a median over ten noisy samples
is not a baseline — least of all in the tail hours where it decides whether a signal is
emitted. 1200 is ~50 days and ~35 samples, still **one** request, sitting exactly at the
ceiling. Features continue to use the most recent 321. **See §9 for what this owes.**

**Commission defaults to 0** and swap defaults to empty. Both are *configured* zeros for
account-specific figures, not missing measurements standing in as ones: Saxo's standard
FX spot pricing is spread-only, and swap rates are published by the broker rather than
derivable from a chart. A plausible-looking guess would be a fabricated cost.

**The shipped calendar claims no coverage and contains no events.** There is no
reachable Saxo calendar, so the YAML is the only source, and no date in it is invented —
a plausible-looking wrong date would blacken the wrong half-hour and, far worse, leave
the right one open. Forex therefore emits nothing until the owner populates it. That is
real recurring manual work and §8 says so.

---

## 8. Fixture provenance — the chain from live API to test (owner requirement R-f)

The spike's 89 raw JSON evidence files were deleted with its throwaway scripts (commit
`4c90819`). Dropping the scripts was right; the raw responses were evidence and should
have been kept. So every Saxo fixture here was hand-authored from
`journal/M10b_SPIKE.md` — from the same document the code was written from, which meant
a misreading in the spike would reproduce into a fixture and the test would pass against
a wrong world. That is how the pip bug nearly survived, and it was the weakest claim in
this milestone.

**Closed on 2026-08-21.** The owner ran
`python -m sentinel.tools.saxo_record_fixtures --login` against the live API and **every
value in the table below matched**. The fixtures are now `VERIFIED` rather than
`RECONSTRUCTED`, and their `_provenance` headers carry the date and what they were
verified against.

**Verified is not recorded, and the two are kept distinct.** These files' values have
been checked against reality; the files are still not captures. The chart fixture's
price *levels* remain invented — the live check confirmed its row keys, window shape and
spreads, not its prices — and `test_the_chart_fixture_still_admits_its_prices_are_invented`
holds it to that. A fixture that let a live check upgrade *everything* in it would be
overstating itself, which is the same class of problem as one that said nothing.
`tests/fx/test_fixture_provenance.py` enforces the whole discipline on every
`saxo_*.json`, and asserts at least four such files exist so an empty glob cannot make
it vacuous.

| Value | Fixture | Source in M10b_SPIKE.md |
|---|---|---|
| Uics 21 / 31 / 42 | `saxo_ref_instruments_*.json` | §4 reference-data table |
| `Format.Decimals` 4 / 4 / **2** | `saxo_ref_details.json` | §4 details table |
| `TickSize` 1e-05 / 1e-05 / **0.001** | `saxo_ref_details.json` | §4 details table |
| `MinimumTradeSize` 1000.0 (all three) | `saxo_ref_details.json` | §4 "Other findings" |
| `AmountDecimals` 2, `LotSize: null`, `LotSizeType: NotUsed` | `saxo_ref_details.json` | §4 "Other findings" |
| Chart row keys (8 price fields + `Time`) | `saxo_chart_EURUSD_60.json` | §2, verbatim key list |
| Window 2026-08-20T22:00Z → 2026-08-21T07:00Z, `DataVersion` 29779589 | `saxo_chart_EURUSD_60.json` | §2 |
| Empty `ChartInfo` / `DisplayAndFormat` | `saxo_chart_EURUSD_60.json` | §2, defect D-h |
| Spread 1.1 pips typical, **2.7 at 21:00** | `saxo_chart_EURUSD_60.json` | §3 hour-of-day table |
| Bid 1.16965 / Ask 1.16977 (1.2 pips, firm) | `tests/fx/test_costs.py` | §3 infoprices control |
| 201 exchange, lifetimes 1070/1182/1200 & 3582/3600 | `tests/ingestion/test_saxo_auth.py` | §1 lifetimes table |
| Publish lag 0–4 s → 30 s grace | `tests/ingestion/test_saxo_adapter.py` | §A2 |
| Friday close 21:00Z, weekend hours **absent** | `tests/fx/test_hours.py` | §6 |
| 4h grid 01/05/…/21Z (EDT) vs 02/06/…/22Z (EST) | `tests/ingestion/test_saxo_adapter.py` | §D-k |
| `Count` ceiling 1200, silent clamp | `sentinel/core/config.py`, adapter tests | §7, defect D-e |

Every row above was confirmed live on 2026-08-21 — with **one correction**, which is
exactly what the exercise was for:

> **`ChartInfo` and `DisplayAndFormat` come back `null`, not as empty objects `{}`.**
> The spike recorded `{}`. It changes no behaviour — they are never read, and precision
> comes from reference data precisely because there is nothing to read here — but the
> fixture now matches reality on a field the spec names, and D-h's wording is corrected
> in `docs/specs/FOREX.md`.

**The OHLC price *levels* in the chart fixture remain invented** around §3's infoprices
control. Only the shape, the window and the spreads carried evidential weight, and only
those were verified. The file says so in its own header.

`python -m sentinel.tools.saxo_record_fixtures` does three jobs: `--login` (browser login
and checks in **one** process, because the refresh token is single-use), `--check`
(verify, write nothing), and the default (re-record as true captures). It is read-only
(`/ref` and `/chart` only), reads credentials from `.env`, and **never runs in
`make check`** — the same posture as the opt-in Postgres tests. `--check` reports
mismatches rather than overwriting them: silently re-recording over a disagreement would
destroy the only evidence that the reconstruction was wrong.

---

## 9. Verified on 2026-08-21 — and what still is not

This section listed five unverified claims when the milestone was built. **Four are now
closed**, by the owner running the live check and the database verification.

### Closed

**1. The fixtures match reality.** `saxo_record_fixtures --login` against the live API:
Uics 21/31/42, `Format.Decimals` 4/4/2, the derived pips, `TickSize`,
`MinimumTradeSize` 1000.0 and the chart row keys all matched. One correction found —
`ChartInfo`/`DisplayAndFormat` are `null`, not `{}` (§8). The pip cross-check that
failure mode A is entirely about is now confirmed against the venue itself, not against
a document.

**2. The 1200-bar tail is settled, and in our favour.** This was the milestone's one
outstanding *expectation*, and the reason it mattered: the spread profile's baseline
rests on it. On all three pairs —

- the 1200-bar request returned **exactly 1200**, so D-e's silent clamp is not in play
  at the ceiling;
- every bar of the shared window agreed between the 1200-bar and 321-bar reads;
- the `TimeframeFeatures` computed from the long tail's **last 321 rows were identical**
  to those from a direct 321-bar request.

So widening the tail buys ~35 spread samples per hour-of-day instead of ~10 and costs
nothing in feature terms, and **D-d's anchor instability does not reach this boundary.**
Had it disagreed, the instruction was to stop rather than work around it; it did not.

**3. The migration is verified against a fresh production dump.** Run by the owner on
the server, `0009 → 0011 → 0009 → 0011`:

```
signals=4  cycles=51  llm_calls=113   — counts identical through up → down → up
```

That is real production scale (M10a's run saw signals=4, cycles=36, llm_calls=83), and
it exercises R-a directly: `ohlcv_candles.volume` becomes nullable, **no crypto row
acquires a NULL**, and the two new tables are created empty.

**4. The database round-trip suite passed against the `0011` schema.** All **60**
opt-in Postgres tests ran and passed — including the five new ones covering
`forex_instruments`, `saxo_oauth_tokens` and a forex candle persisting with a null
volume, which is the case migration `0011` exists for and the only one that could not be
proved against the in-memory doubles.

### Still not verified

**No live cycle, and no live forex read beyond the check above.** The adapter has
fetched real candles now, but nothing has run a forex *cycle* — there is no forex cycle
to run until M10b-2 wires one, and a crypto cycle costs money. The costs, the sizing and
the gates have been exercised only against fixtures and hand-computed values.

**`spread_max_multiple: 3.0` is still a guess.** Sanity-checked against the spike's
distributions and nothing more. It is calibrated from DRY_RUN data or it is not
calibrated at all, and DRY_RUN needs M10b-2 and M10c first.

**The swap table is empty and commission is zero.** Both configured, neither measured —
see §7. Any net-RR figure produced today therefore prices the spread and nothing else,
which is correct for Saxo's spread-only FX pricing but will understate the cost of any
position held overnight until the swap rates are filled in.

## 10. Migration `0011`, verified both directions

**Against a fresh production dump, by the owner, on the server** —
`signals=4 cycles=51 llm_calls=113`, counts identical through `up → down → up`. The run
below is the local rehearsal against a dev dump; the production run is the one that
counts and it agreed.

```
restored at 0009_watchlist_requests · signals=1 cycles=9 llm_calls=21 gate_decisions=3 analyst_reports=11
step 2/6 — upgrade head              0009 → 0010 → 0011
backfill OK — every row in 10 tables is 'crypto'
0011 OK — volume is nullable, no crypto row lost one, forex_instruments saxo_oauth_tokens created empty
row counts unchanged
step 3/6 — downgrade to 0009         0011 → 0010 → 0009
downgrade OK — columns dropped, row counts unchanged
step 4/6 — upgrade head again        0009 → 0010 → 0011
0011 OK — volume is nullable, no crypto row lost one, …
MIGRATION OK
```

The script's target moved to `0011` while its floor stayed at `0009` **deliberately**,
so the round trip exercises both migrations rather than only the new one. Two assertions
were added for owner requirement R-a: `ohlcv_candles.volume` is nullable and **no crypto
row acquired a NULL**, and the two new tables exist and are empty. `DROP NOT NULL` is
catalogue-only, so nothing was rewritten on the largest table in the schema.

The downgrade deletes forex candles before restoring NOT NULL, because by then rows may
legitimately have no volume. They are re-fetchable in one request per timeframe from a
venue holding ~50 days of hourly history, and they belong to a market the shipped config
disables — a downgrade that *fails* would be the worse outcome.

The local dev environment survived intact: `127.0.0.1:5432` still published, container
untouched (journal/M10a_REPORT.md §3b's hazard).

---

## 11. Numbers

* **1777 passed, 60 skipped, 0 failed** in the hermetic suite — up from 1561/55,
  **225 new tests**. The 60 skips are the opt-in Postgres tests, and the owner ran all
  60 against the `0011` schema on 2026-08-21: **0 failed**.
* `make check` **exit 0**: tests, ruff, mypy `--strict` over 266 files, 100% risk branch
  coverage, **`check-deps`** (new), `check-ops`, wheel build, image build **and in-image
  import** — which now also asserts the forex calendar ships in the wheel.
* Local venv and the built image both at `anthropic 0.125.0`, `httpx 0.28.1` — the
  versions production runs.
* `sentinel/risk/` and `sentinel/analyst/prompts/`: **zero-line diff.**
* Golden fixtures: **23 files, all byte-identical.**
* 20 files modified (+1130 / −46); ~6,560 lines of new forex code, tests and fixtures.

New test files: `tests/fx/` (137 tests across instruments, hours, sizing, costs, spread,
calendar, tails and fixture provenance), `tests/ingestion/test_saxo_adapter.py` (21),
`test_saxo_auth.py` (26), `test_volume_invariant.py` (8),
`test_forex_persistence.py` (5, opt-in), `tests/core/test_forex_degrades_only.py` (11),
`tests/test_dependencies.py` (7).

Live verification, all owner-run on 2026-08-21: the C3 tail check and the fixture
comparison via `saxo_record_fixtures --login`; `ops/verify-migration.sh` against a fresh
production dump; and the 60-test Postgres suite against `0011`.

---

## 12. What did NOT ship, and where it goes

**M10b-2 — features, charts, wiring (steps 7–8).** Prior-day and prior-week levels,
session labels, the synthetic USD strength index, cross-pair correlation and the
measured spread series as features; the chart variant with **no volume panel at all**
rather than an empty one; the forex analyst prompt stating plainly which inputs are
unavailable (the second half of §2.1's test); and the orchestrator and tracker wiring.

One finding to carry into it: the volume panel must be decided **from the data**, never
from a new `ChartSpec` field. `ChartRenderParams.to_json_dict()` dumps the spec, so any
new field there changes the crypto chart golden — a forex change moving a crypto golden,
which is by definition the wrong change.

Also deferred to M10b-2: **crypto's reserved budget floor of 8.00** (owner decision 6,
M10a's open question R5). It is a change to a live spend rail and it only bites once
forex can spend, so it belongs with the wiring rather than ahead of it.

**M10c — card, publishing, tracking.** Defect #12's consequence. Also the home of §11's
**deliberate** regeneration of the golden surfaces, as a named step: switching forex on
gives crypto cards their market tag, which changes their bytes. That must never happen
quietly during a build.

---

## 13. Deploying this

**Not deployed, and not committed.** When you are ready:

`ops/update.sh` will hit the same `git pull --ff-only` conflict journal/M10a_REPORT.md
§9 describes if the server's `config.yaml` is still edited in place. The remedy is
unchanged:

```
cd /opt/sentinel && git checkout -- config.yaml && ops/update.sh
```

First boot applies `0011`: one `ALTER COLUMN … DROP NOT NULL` (catalogue-only) and two
new empty tables. The app then loads a config whose `markets.forex.enabled` is still
`false`, registers one `scan:crypto` job at 60 minutes, and runs a cycle behaviourally
identical to the one before it. Nothing under `forex:` is reachable.

Before that, on the server: run `ops/verify-migration.sh` against a fresh production
dump (§9 item 3).
