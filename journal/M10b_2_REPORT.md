# M10b-2 — forex features, charts and wiring

**Date:** 2026-08-21. **Status:** complete, `make check` green, **not deployed.**

Build-order steps 7 and 8 of `docs/specs/FOREX.md` §14: the deterministic features,
the chart variant, the adapter registry, the forex analyst prompt, crypto's reserved
budget floor, and an orchestrator path that runs a forex cycle as far as an analyst
report. Everything is behind `markets.forex.enabled: false`, so **deploying this is a
no-op for the running crypto system** — §11 says what that claim rests on.

**Read §2 first.** It is a defect this milestone found in the last one, and its shape
matters more than the fix.

---

## 1. The binding constraint held

```
$ git diff --stat sentinel/risk/ sentinel/analyst/prompts/fable_v1.md \
                  sentinel/analyst/prompts/screener_v1.md sentinel/analyst/prompts/screener_v2.md
(no output)
```

All 23 golden fixtures **byte-identical** by sha256 — checked before the first edit and
after every change, not once at the end. Risk-engine branch coverage unchanged at
**100%**: 604 statements, 152 branches, the same figures as journal/M10a_REPORT.md and
journal/M10b_REPORT.md.

The chart renderer is where the crypto goldens live, and it is the module this milestone
had to change most. §4 is about how.

---

## 2. M10b-1 shipped an adapter whose output could not be drawn

**And all 1777 of its tests passed.**

```
ValueError: Axis limits cannot be NaN or Inf
  mplfinance/plotting.py:705: RuntimeWarning: All-NaN slice encountered
```

`OHLCVSeries.to_frame` puts `NaN` — never `0.0` — in the volume column when candles
carry none, which is correct and is exactly what §2.1 asks for. `charts/renderer._draw`
passed `"volume": True` unconditionally, which was correct for every market that existed
when it was written. **The first forex chart ever rendered was rendered in this session,
and it raised.**

Neither half was wrong. The adapter was tested; the renderer was tested; the *join*
between them was not, because the renderer belonged to the next session.

### The shape of it

This is the fourth instance in three days of one pattern, and the first three are in
journal/M10b_REPORT.md §3b:

| | looked like success | was |
|---|---|---|
| `make check-wheel` | wheel built cleanly | app could not import |
| the token exchange | HTTP 2xx, "saved" printed | nothing persisted |
| `alembic upgrade head` | exit 0 | nothing migrated |
| **M10b-1's suite** | **1777 passed** | **every piece worked alone** |

A green suite is a real signal that did not mean what it was taken to mean. What it
actually meant was "each component works in isolation", and nothing had asked the other
question.

**The rule, now in FOREX.md's corrections log: a milestone boundary is a place where
nothing is tested by construction. When a boundary splits a producer from its consumer,
the next milestone composes them first and builds second.**

`tests/core/test_forex_cycle.py::test_the_whole_forex_path_composes_from_adapter_to_rendered_chart`
is that composition: the real Saxo adapter over a synthetic venue, through the real
feature engine, into the real renderer, asserting on the bytes that come out. It would
have caught this on the day it was introduced.

**§12 names the four joins M10b-2's own boundary with M10c leaves untested**, for
exactly the same reason. They are the first thing M10c should compose.

---

## 3. Four more spec defects, all found reading FOREX.md against the code

All four are recorded dated in `docs/specs/FOREX.md`'s corrections log. Owner rulings
are dated 2026-08-21. Two of them were gaps rather than errors — §6 asks for things it
never defines — and one was a proposal of mine that the owner rejected, correctly.

### #17 — the session labels have no definition (§6)

§6 asks for "Tokyo / London / New York / the London-NY overlap" and gives no hours and,
more importantly, no timezone basis.

**Ruling: DST-aware local windows** via stdlib `zoneinfo` — Tokyo 09:00–18:00
`Asia/Tokyo`, London 08:00–17:00 `Europe/London`, New York 08:00–17:00
`America/New_York`. The UTC hours are an output, never an input.

This is D-k's lesson applied to sessions, and the numbers make the case:

| period | London-NY overlap |
|---|---|
| both on standard time (January) | 4 hours |
| both on summer time (July) | 4 hours |
| **US on EDT, EU still on GMT (8–29 March 2026)** | **5 hours** |
| **EU back on GMT, US still on EDT (25 Oct – 1 Nov 2026)** | **5 hours** |

A config table of UTC hours would be right for forty-four weeks a year and silently
wrong for the other eight. `test_the_utc_hours_move_when_new_york_changes_daylight_saving_and_london_has_not_yet`
pins all four rows.

New York's 17:00 close is also the instant Saxo anchors its daily bar to, so
`test_new_yorks_close_is_the_same_instant_saxo_anchors_its_daily_bar_to` asserts the
session boundary and the bar boundary agree in **both** seasons — 21:00Z in August,
22:00Z in January. If they ever disagree, one of the two has a hardcoded UTC hour in it.

Tokyo never moves, which is why the Tokyo-London gap breathes rather than the Tokyo
session doing so.

### #18 — the USD index and the correlation have no definitions either (§6)

**Ruling:** USD legs `1/EURUSD`, `1/GBPUSD`, `USDJPY`; index = equal-weight
**geometric** mean rebased to 100, on 1h closes over the 321-bar feature window;
correlation = Pearson on **log returns**, 120 bars, pairwise.

Both choices are pinned by the property that distinguishes them from the alternative,
not by a magic number:

- **Geometric, not arithmetic.** If EURUSD halves and GBPUSD doubles, the dollar has
  strengthened enormously against one and weakened equally against the other, and an
  equal-weight index should not move. The geometric mean gives exactly 100. An
  arithmetic mean of percentage changes gives +25%, which is an artefact of the
  arithmetic and not a fact about the dollar.
- **Log returns, not prices.** Two series that both drift upward correlate near 1 on
  prices whatever their day-to-day behaviour — which would make §9's correlation rail
  look satisfied by arithmetic. `test_correlation_is_on_log_returns_not_prices` uses two
  series with a shared uptrend and deliberately opposed returns.

**A structural point §6 does not acknowledge:** both are **cross-symbol**, and
`features.engine.compute()` is per-symbol and market-blind. They are therefore computed
**once per cycle** in `sentinel/fx/features.py` and shared by all three snapshots. The
market-blind engine is untouched — it is the module the crypto golden is computed from.

Two thirds of a dollar index is not a dollar index: a missing pair yields `None`, never
a partial index computed from two while claiming three.

### #19 — the prompt filename convention has no market axis (ENSEMBLE.md §2)

**Ruling: `fable_forex_v1.md`.** Full rationale in `journal/PROMPT_LOG.md`.

### #20 — the chart annotations had nowhere to be recorded, and my proposal was wrong

I proposed drawing the forex reference lines and session bands **without** recording them
in `ChartRenderParams`, deferring the record to M10c, on the grounds that forex reaches
no signal row until then so nothing loses provenance today.

**The owner ruled against it, and the reasoning is worth keeping**: that argument is true
only until the day forex *does* reach a signal row, at which point the gap is already in
the code and nobody remembers it was deliberate. A stored chart that draws a line nothing
in the database explains breaks the property M3 exists for.

**Resolved by conditional serialisation** (the owner's mechanism, and it works):

```python
@model_serializer(mode="wrap")
def _omit_empty_annotations(self, handler):
    data = handler(self)
    if not self.annotations:
        data.pop("annotations", None)   # crypto: the key never appears
    return data
```

Crypto's `to_json_dict()` is byte-identical to what it was before the field existed —
the key is **absent**, not `[]` and not `null` — and forex's carries every line and band
it drew. `mode="wrap"` rather than an edit to `to_json_dict`, so every serialisation path
is covered including a nested dump of the enclosing `ChartImage`, which nobody writes
today and is exactly the sort that appears later.

Three tests hold it: the key is absent on a crypto record; it is **present** on a forex
one (the non-vacuity sibling — a field that never serialised would pass the first test
just as well); and a forex record re-renders the same bytes.

`ChartAnnotation` is a new type rather than a reuse of `DrawnLevel`, because `DrawnLevel`
carries a `touches` count and a prior-day high has no touch count. A zero there would be
the fabricated number §2.1 exists to forbid — the same defect class as #12. A validator
makes the mistake unrepresentable: a band carries an interval and no price, a line
carries a price and no interval, and neither can carry a zero for the other.

---

## 4. The renderer, and how crypto's bytes did not move

Three rules governed every change in `sentinel/charts/`:

1. **No field on `ChartSpec`.** It is nested inside every params record, crypto's
   included. The volume panel is decided **from the data** — `window["volume"].notna().any()`
   — which is what journal/M10b_REPORT.md §12 said this milestone had to do.
   `test_the_volume_decision_is_read_from_the_data_not_from_the_spec` renders the *same*
   spec against a crypto and a forex series and gets two different pictures.
2. **`RENDERER_VERSION` not bumped.** It is itself a params field, and its own docstring
   says it is bumped "whenever a change alters rendered pixels". No crypto pixel moves.
3. **The two-panel branch of `_claim_canvas` is untouched, character for character.**
   The no-volume case is a new early return. With volume, mplfinance returns four axes
   (two visible panels and their invisible twins); without it, two — which is why the
   kwargs have to be *absent* rather than `False`-valued.

`render()` gained one runtime kwarg, `annotations`, defaulting empty. Runtime kwargs are
not in the reconstruction record, so every existing call site is untouched.

### One thing worth flagging: a formatter that could not be shared

`_format_price` rounds anything above 1 to two decimals. On a EURUSD chart that renders
**every** level as "1.17" — prior-day high, prior-day low and daily open all identical
and all useless.

It could not simply be fixed. `_format_price` labels crypto's S/R lines, and changing its
`1 <= price < 1000` branch would move the bytes of every crypto chart quoted in that
range — LINK, AVAX, LTC — which **the golden would not have caught**, because the golden
is BTCUSDT at 64,100 and takes the `>= 1000` branch. A silent change to a live market's
stored charts is exactly what this milestone is forbidden to make.

So `_format_price` is untouched and a second formatter is selected from the same
data-derived signal the panel is: no volume means forex means five decimals. The comment
in the code says plainly that the branch is not really about volume — it is that volume
absence is the only market signal the renderer has from the data, and it is already
load-bearing for the panel.

---

## 4a. The golden had a hole, and it is now closed (owner requirement H1)

§4 records that `_format_price` could not be shared with the forex path. The reason
it could not — *changing it would move crypto chart bytes the golden would not
catch* — is a finding about the safety net rather than about the formatter, and the
owner is right that it is the most important thing in this milestone.

**The hole.** `charts.json` pinned BTCUSDT and nothing else. `_format_price` has
three branches:

```python
if price >= 1000:  return f"{price:,.0f}"     # BTCUSDT — the only one pinned
if price >= 1:     return f"{price:,.2f}"     # LINK, AVAX, LTC, SOL — unpinned
return f"{price:.6g}"                          # XRP, DOGE, ADA — unpinned
```

One symbol pinned one branch. A change to the middle one would have silently moved
the stored chart bytes of **three live watchlist symbols**, and the suite — the same
suite M10a, M10b-1 and M10b-2 have all relied on to prove crypto did not move —
would have stayed green.

**The fix is an ADDITION, not a regeneration.** Two more golden symbols, one per
uncovered branch, both from the live watchlist:

| symbol | last close | branch | added |
|---|---|---|---|
| BTCUSDT | 77,131.8 | `>= 1000` | M10a — the only one there was |
| SOLUSDT | 90.83 | `1 .. 1000` | M10b-2 |
| DOGEUSDT | 0.08374 | `< 1` | M10b-2 |

XRPUSDT was checked first and rejected: at 1.3766 it sits in the **same** branch as
SOLUSDT and would have added a fixture without adding coverage.

**The 23 existing fixtures were not regenerated and are byte-identical** —
`git diff --name-only tests/fixtures/golden*` is empty across both commits. Four
files were added: `features_SOLUSDT.json`, `charts_SOLUSDT.json`,
`features_DOGEUSDT.json`, `charts_DOGEUSDT.json`.

The extra symbols have their own generator,
`tests/fixtures/generate_goldens_m10b2.py`, whose write-set is derived from
`EXTRA_SYMBOLS` alone — BTCUSDT's six fixtures are not in it and cannot be reached
from there. Refreshing an extra symbol therefore cannot overwrite the fixtures whose
whole job is to prove crypto output did not move.

**DOGEUSDT needed cassettes that did not exist.** Only BTCUSDT and SOLUSDT had ever
been recorded. `sentinel/tools/record_cassettes.py` recorded all of its symbols on
every run, so adding one would have refreshed the other two with fresh market data
and moved every golden at once — *a regeneration wearing an addition's clothes*. It
gained `--symbols` and `--skip-http`, and the DOGE fixtures were recorded with
`--symbols DOGEUSDT --skip-http` from the public keyless Binance endpoints, exactly
as the other two were. `git status` confirmed no existing cassette was modified.
The recording date differs from the others' (2026-08-21 vs 2026-08-18) and
`tests/cassettes/README.md` says so — nothing compares two symbols against each
other, so that is not a problem; it is the evidence that the addition was an
addition.

**Proved, not assumed.** Each branch was perturbed in turn and the golden suite run:

| perturbed branch | golden that failed |
|---|---|
| `>= 1000` — `,.0f` → `,.1f` | `test_chart_bytes_are_unchanged` (BTCUSDT) |
| `1 .. 1000` — `,.2f` → `,.3f` | `..._extra_symbols_chart_bytes_are_unchanged[SOLUSDT]` |
| `< 1` — `.6g` → `.5g` | `..._extra_symbols_chart_bytes_are_unchanged[DOGEUSDT]` |

**Exactly one golden failed each time, and a different one each time.** Before this
work the second and third rows were both blank: the entire suite was green for a
change that altered the stored charts of six live watchlist symbols.

`test_every_formatter_branch_is_covered_by_exactly_one_golden_symbol` asserts the
**partition** rather than the symbols, so the set cannot quietly collapse: swapping
an extra symbol for another large-cap fails that test instead of silently covering
one branch three times.

**What the extra symbols do not cover, and why.** Not the gate and not the card.
`bot/cards.py` interpolates prices the risk engine has already quantized, so nothing
there is magnitude-sensitive; a second hand-tuned analyst report would add a fragile
golden without covering the hole this exists to close.

**The lesson, in HANDOFF §4 item 10:** a golden pins the case it was built from, and
nothing else. When adding one, ask which branches of which functions the chosen case
actually reaches. Coverage is of inputs, not of code — and one input can only ever
reach one branch of a conditional.

## 5. Features (step 7)

`sentinel/fx/sessions.py` and `sentinel/fx/features.py`. Pure, stdlib `zoneinfo` only —
no new dependency, and `check-deps` confirms (53 third-party imports seen, all declared).

**Added:** prior-day and prior-week high/low/open/close, the daily and weekly open,
session labels for both "now" and the last bar, the synthetic USD strength index, rolling
cross-pair correlation, and the measured spread with **both** its baselines (defect #15:
the gate's global median and the cost model's per-hour median are different numbers
answering different questions).

**Boundaries are read from the data, then cross-checked against the clock.** The week
boundary comes from the hole in the candle series — closed hours are cleanly absent,
which the spike measured — so it needs no config value and moves with DST on its own. The
daily anchor is derived from `America/New_York` and then **asserted against the 4h grid
the venue actually sent**:

```
EURUSD: the daily anchor derived from America/New_York is 21:00Z, which is not in
the 4h grid the venue sent (02, 06, 10, 14, 18, 22). One of the two is wrong and
levels an hour out would still look plausible (D-k).
```

Same shape as the pip's `TickSize × 10` cross-check and for the same reason: two
independent derivations that must agree, so a wrong one fails loudly instead of producing
levels that are quietly an hour out.

**`ForexConfig.feature_candles_1h` finally has a reader.** It has been a config key with
no consumer since M10b-1. The 1200-bar 1h tail feeds the spread profile (~35 samples per
hour-of-day instead of ~10); the most recent 321 feed the features, which the owner
verified live on 2026-08-21 to be identical to a direct 321-bar request.

### §2.1, and where it is actually tested

`ForexFeatures` has **no volume field, no funding field, no open-interest field at all** —
the stronger form of the rule, the same one `ForexSizing` is held to. And the payload
*states* the absence rather than merely containing it: `unavailable_inputs` names all
four, so a reader — human or model — sees a statement instead of an unexplained silence.

The §2.1 test deliberately runs on the case that actually breaks: a symbol whose tails
did **not arrive**. That is where a naive implementation produces a 0.0 high, a 0-pip
spread and a 0.0 correlation, every one of which reads as a measurement. All of them must
be `None`, and a recursive walk asserts no numeric zero survives anywhere in the dumped
payload. `test_the_zero_walk_would_catch_a_zero_if_one_were_there` plants one, because a
check never seen to fail is indistinguishable from one that cannot.

### Forex features reach the analyst without touching crypto's payload

They ride **inside** `MarketSnapshot.features`, which is already a free dict, rather than
as a new snapshot section. A new section would have to join
`analyst/serialization.PASSTHROUGH`, which dumps with `include=` — so crypto's payload
would gain `"forex": null` and the crypto prompt golden would move. Cost to crypto: zero.

---

## 6. Wiring (step 8)

### The adapter registry

`MarketConfig.adapter` has named an adapter by string since M10a and **nothing resolved
it**: `core/wiring.py` hardcoded `BinanceCryptoAdapter` in two places and annotated its
return type as that concrete class. `adapter: forex_saxo` in the shipped config named
real, tested code and no behaviour at all.

`ADAPTERS` now maps both names, and `build_adapter` raises `UnknownAdapter` on anything
else. **The property M10a was protecting by leaving `forex_saxo` unregistered survives**,
and it is the case that actually happens — a typo:

```
market 'forex' names adapter 'forex_sax0', which nothing implements
(known: crypto_binance, forex_saxo)
```

Falling back to the crypto adapter would ingest Binance candles for EURUSD and store
them as forex: a corrupted population rather than an outage, and the kind only discovered
by the numbers being wrong months later. A test also asserts **every configured market,
enabled or not**, names an adapter the registry knows — an unbuildable name behind
`enabled: false` is a trap set for switch-on day.

### One scan job per enabled market

Already true since M10a; nothing pinned it. Now
`test_with_forex_disabled_exactly_one_scan_job_is_registered_at_sixty_minutes` asserts
the job set is exactly `{scan:crypto @ 3600s, tracker @ 60s}`, with a non-vacuity sibling
that enables forex in a test-only config and gets two scan jobs.

The tracker stays crypto-only. Its comment now names *when* that gap closes — M10c —
rather than leaving it as a standing note.

### Crypto's reserved budget floor of 8.00 (FOREX.md §13 decision 6)

The sub-budgets sum to 14 under an 11 ceiling on purpose, so the markets compete for the
last dollar. That is right between two *measured* markets and wrong the day an unmeasured
one joins a measured one, which is precisely the situation forex creates.

`MarketConfig.llm_reserved_floor_usd`, crypto set to `8` (written `8`, never `8.00` — the
YAML-float hazard the config file already warns about). **Only the unspent part of a floor
is held**: crypto reserves 8.00 at the start of the day and nothing once it has spent it,
so the competition resumes the moment the floor has done its job. `SpendScope` gains a
`RESERVED` member, because "forex hit its own budget" and "forex was held back for crypto"
read almost identically on a status card and are answered by different levers.

**This is a change to a live spend rail while that rail is running, so the load-bearing
test is that it changes nothing today.** With forex disabled it spends nothing and
reserves nothing, so crypto's effective ceiling stays the full 11 and every verdict is
what it was before — asserted across the whole ladder from $0 to $11.

Two config rails fail at load: a floor above its own market's budget (it would reserve
money that market cannot spend), and floors summing past the global ceiling (a deployment
that could never honour its own promises).

**One finding worth recording.** `tests/test_config.py` asserts the repo config and the
frozen pre-M10a deployed file describe an identical crypto market. They now differ in
exactly one field — the floor — because `docs/DEPLOY.md` §6/§13 edit `config.yaml` in
place and the deployed file predates `markets:` entirely. That difference is named in the
test rather than excluded quietly, and
`test_the_legacy_deployed_config_behaves_identically` proves it is behaviourally
invisible: a floor only ever reserves against *other* markets, and a legacy config has
exactly one.

### The forex cycle path

Forex does **not** go through `SnapshotAssembler`, and the two reasons are not stylistic:

1. **Its staleness rule is a different rule.** Spec defect #14: `ingestion/staleness`
   measures from `fetched_at`, which is always ~now, so applied to forex it would call a
   fifty-hour-old weekend tail perfectly fresh. Forex measures from the **candle**, and
   that check must be skipped while the market is shut. Both halves are tested, including
   `test_the_same_stale_tail_over_a_weekend_is_closed_not_stale` — identical data, and it
   reads as a closed market rather than a fault.
2. **Bid and ask must be one read.** D-d found the same hour present or absent depending
   on the request's anchor, so a spread measured across two reads of "the same" window is
   a real hazard. `adapter.ohlcv()` returns the bid alone, so the forex path uses
   `fetch_tail` and derives snapshot, features and spread from that single read. Twelve
   chart requests per cycle, not twenty-four, and a test counts them.

`SkipReason` gains `MARKET_CLOSED` and `NO_DATA`. `MARKET_CLOSED` is its own key rather
than folded into `NO_DATA` because §5.1 says closed is a normal state with its own reason
code, and forex is shut about 49 hours a week — "how often was the market simply shut"
has to be answerable in SQL separately from "how often did the data fail to arrive". The
column is JSONB, so no migration. A pre-existing test that asserts every `SkipReason` has
user-facing wording caught the omission immediately, which is the guard working.

**Where the cycle stops: at an analyst report.** `sentinel/risk/`'s gate produces a
`TradePlan`, which is crypto-shaped (defect #12), and §7.6 forbids faking the liquidation
buffer. So `_analyse_forex_symbol` records the report and logs
`cycle.forex_stops_at_report`. It does **not** fall through to `_gate_and_publish`: that
path would read a forex report through crypto's sizing and produce numbers wrong in a way
nothing downstream could notice. The dispatch is a single early return in `_run_cycle`,
so the crypto path below it is the code it was before this milestone.

---

## 7. Numbers

* **1902 passed, 60 skipped, 0 failed** — up from 1777, **125 new tests**.

  | file | tests |
  |---|---|
  | `tests/fx/test_sessions.py` | 17 |
  | `tests/fx/test_features.py` | 28 |
  | `tests/charts/test_forex_variant.py` | 14 |
  | `tests/core/test_forex_cycle.py` | 20 |
  | `tests/llm/test_reserved_floor.py` | 17 |
  | `tests/core/test_adapter_registry.py` | 7 |
  | `tests/core/test_scan_jobs.py` | 5 |
  | `tests/analyst/test_prompts.py` | 33 (was 23) |
  | `tests/golden/` (both modules) | 37 (was 30) — two extra golden symbols |

* `make check` **exit 0**: tests, ruff, `mypy --strict` over 277 files, 100% risk branch
  coverage, `check-deps`, `check-ops`, wheel build, image build and in-image import —
  which now also asserts **every configured market's** analyst prompt ships in the wheel,
  following M10b-1's calendar precedent.
* `sentinel/risk/` and the three existing prompt files: **zero-line diff.**
* Golden fixtures: **23 existing files byte-identical**, plus **4 added** for the
  two extra golden symbols (§4a) — an addition, not a regeneration. Ten new
  DOGEUSDT cassettes; no existing cassette re-recorded.
* 23 files modified, 28 added. ~2,200 lines of new forex code, tests and documentation.
* **No `pyproject.toml` change.** `zoneinfo` is stdlib; `numpy` and `pandas` were already
  declared.

---

## 8. Contract changes, named

Four, all additive, none touching the frozen package. Recorded because CLAUDE.md asks to
be consulted before altering a Pydantic contract, and three of these were directed by the
milestone brief or an owner ruling.

| change | why | authority |
|---|---|---|
| `ChartRenderParams.annotations` (conditionally serialised) | the reconstruction record must be complete | owner ruling G1, 2026-08-21 |
| `MarketConfig.llm_reserved_floor_usd` | crypto's reserved floor | FOREX.md §13 decision 6 |
| `MarketConfig.analyst_prompt_version` | per-market prompt selection; follows `adapter: str` | milestone brief, defect #19 |
| `SkipReason.MARKET_CLOSED` / `.NO_DATA`, `SpendScope.RESERVED` | new states that genuinely exist | this milestone |

`Candle`, `OHLCVSeries`, `SymbolFeatures`, `TimeframeFeatures`, `ChartSpec` and every
model in `sentinel/risk/` are unchanged. **No migration.** `cycles.skipped` is JSONB and
the new enum members need no schema change.

---

## 9. Deliberate deviations, and why

**One unified `annotations` kwarg rather than two.** The plan proposed separate
`reference_lines` and `session_bands` parameters. One type serving as both the input and
the record is what makes the params report *what was drawn* rather than what was
requested — an off-chart line is skipped exactly as an off-chart S/R level is, and
`test_the_record_reports_what_was_drawn_not_what_was_asked_for` holds it there.

**No `timeframes` injection into `SnapshotAssembler`.** The plan had one; it was written
and then reverted, because once forex stopped going through that class (§6) nothing read
it, and an unused parameter is dead code that reads as a feature.

**Session bands are generated across every charted timeframe's span**, not the 1h one: a
120-bar 4h chart reaches twenty days back where a 120-bar 1h chart reaches five. Taken
from the snapshot rather than the raw tail, so the 1200-bar spread tail does not produce
fifty bands for a chart that can show four.

**No screener on the forex path.** `screener_v2` asks about funding, open interest and
relative volume — three things this market does not have. Running it would be asking a
crypto question about a forex snapshot, and with three symbols there is no cost argument
for it. All three are analysed.

---

## 10. What is NOT verified

**No live forex cycle has ever run.** Everything here is exercised against a synthetic
venue (`tests/core/saxo_double.py`), which serves `/ref` from the recorded fixtures and
generates `/chart` responses on the venue's real grids — the August 4h anchor, weekends
absent, the newest bar still forming. That is enough to prove the pieces compose. It is
**not** evidence about the market, and the file says so in its own docstring.

**The prompt has never been sent to the model.** No NO_SETUP rate, no token count, no
cost figure. `journal/PROMPT_LOG.md` says what to watch when it is first run.

**The synthetic prices are invented.** The correlations and the USD index in the tests are
facts about the generator, not about EURUSD. Only their *properties* — geometric
cancellation, log-return sensitivity, absence rather than zero — carry weight, and those
are what the tests assert.

**Still open from M10b-1, unchanged:** `spread_max_multiple: 3.0` is an uncalibrated
guess; the swap table is empty and commission is zero, both configured rather than
measured; and `numpy` is still unpinned underneath `matplotlib`, `mplfinance` and every
indicator value.

---

## 11. Why deploying this is a no-op

`markets.forex.enabled` is `false`. Therefore:

- `enabled_markets` is `(crypto,)`, so exactly one `scan:crypto` job is registered at 60
  minutes — asserted, not assumed.
- `CycleOrchestrator` is only ever constructed with `market=crypto`, so the forex branch
  in `_run_cycle` is unreachable.
- Crypto's spend verdicts are identical: forex spends nothing and reserves nothing, so
  crypto's effective global ceiling stays the full 11.
- `config.multi_market` is still `False`, so no card gains a market tag.
- Every golden — cycle and surfaces — is byte-identical.
- `test_a_crypto_cycle_never_touches_a_saxo_adapter_or_credential` still passes: it makes
  constructing a Saxo adapter or credential an error for the duration of a full crypto
  cycle, and the cycle completes.

**No migration runs.** Nothing in this milestone touches the schema.

---

## 12. What did NOT ship, and where it goes

**M10c — the card, publishing, tracking, and the sizing join.** Defect #12's consequence.
A forex cycle currently ends at a stored `analyst_reports` row.

**The four joins this milestone's own boundary leaves untested by construction** (§2's
rule, applied to myself). M10c should compose each of these *before* building on it:

1. A forex `AnalystReport` has never reached `sentinel/bot/cards.py` — the same
   composition seam that produced defect #12.
2. A forex `ChartRenderParams` has never been persisted into a `signals.chart_params`
   row, so the annotations recorded under decision #20 are proven by unit test and not by
   a round trip through Postgres.
3. M10b-1's `ForexSizing` has never been fed entry and stop levels from a real analyst
   report — only from hand-computed fixtures.
4. `TrackerLoop` has never seen a forex symbol.

**The named future step, and it must not happen quietly.** When forex is switched on,
`config.multi_market` becomes `True` and crypto cards gain their market tag — which
changes crypto's card bytes and therefore the golden surfaces. **Those goldens must be
regenerated deliberately at switch-on, as a named step in that milestone, never as a side
effect of a build.** §11 of FOREX.md says so; this is the second place it is written down.

**Not deferred but worth naming:** central-bank RSS news (§10) is not wired — the forex
snapshot carries no news at all, and the prompt's untrusted-news fence is therefore empty
today. The fence and its rule ship anyway, because the day news is added is not the day to
discover the rule was missing.

---

## 13. Deploying this

**Not deployed, and not committed.** When you are ready, the M10a §9 caveat is unchanged:

```bash
cd /opt/sentinel && git checkout -- config.yaml && ops/update.sh
```

First boot applies no migration. The app loads a config whose `markets.forex.enabled` is
still `false`, registers one `scan:crypto` job at 60 minutes, and runs a cycle
behaviourally identical to the one before it. Nothing under `forex:` is reachable.

### The server's `config.yaml` has no `markets:` block — a named step, not a note

`docs/DEPLOY.md` §6 and §13 edit `config.yaml` in place on the server, so the deployed
file still predates M10a. It is read as crypto-only, which is correct and is why nothing
has broken. Two consequences, both now written into **`docs/DEPLOY.md` §13b** as an
explicit procedure rather than left here as a warning:

1. **The server carries no `markets.crypto.llm_reserved_floor_usd`.** Crypto's reserved
   floor is in this repo and not on that machine. Harmless while crypto is the only
   market that can spend, and **not** harmless the moment forex can. Config load will
   not complain — a missing floor is a valid zero — so nothing will tell you.
2. **Forex cannot be enabled by flipping a flag there**, because there is no flag on
   that machine. Enabling it means writing the whole `markets:` block, and **the floor
   has to go in with it**, not after.

`ops/update.sh` runs `git pull --ff-only`, which **fails whenever a commit also changes
`config.yaml`** — and M10a, M10b-1 and this milestone all did. The symptom is
`error: Your local changes to the following files would be overwritten by merge`. The
remedy is in DEPLOY §12 and it starts with `git diff config.yaml`: **read the server's
edits before discarding them.** On a live server that diff contains `dry_run: false`,
and dropping it without re-applying puts the system silently back into rehearsal.

DEPLOY §13b also records what forex needs that this deployment has never had —
`SAXO_APP_KEY`, `SAXO_APP_SECRET` and a one-time browser login whose refresh chain has a
one-hour memory (FOREX.md §3.1) — and repeats the golden-surface regeneration step, so
the person at the terminal on switch-on day does not have to have read this report.
