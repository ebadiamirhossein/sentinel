# M10e — the forex daily-staleness rail

**Branch:** `m10e-forex-daily-staleness` off `main` (`e5fa421`)
**Written:** 2026-08-24

Forex was switched on at M10d and produced nothing. This milestone measured why, fixed
the rule that caused it, added the test that would have caught it, and gave a market that
ingests nothing a voice.

---

## State at handoff

**What changed:** the 1d staleness rail is measured in observed market hours instead of
wall-clock time; the intraday rails are untouched; no threshold moved; there is a new
`forex.daily_freshness` log line every cycle and a new owner alert when a market ingests
nothing for three cycles running.

**Not deployed.** `make check` is green. The image builds and imports.

### The dated prediction, and why tomorrow cannot test it

Before the fix, forex recovered on its own every Tuesday and broke again every Monday.
That is the strongest claim in this milestone and it is falsifiable — but **you deploy
before Tuesday, so Tuesday's evidence is post-fix and settles nothing.**

Tuesday 2026-08-25 passes under *both* rules. The newest closed daily bar is Monday's,
31 hours old by the clock and 30 by the market: under 48 either way. **A green Tuesday
proves only that the fix broke nothing.** The first cycle that can distinguish the two
rules is **Monday 2026-08-31**.

There is one query that tells them apart, and it works on any post-deploy cycle:

```bash
docker compose logs --since=2h app | grep forex.daily_freshness | jq -c '{symbol, observed_hours, wall_clock_hours, margin_hours}'
```

| what you see | what it means |
|---|---|
| `wall_clock_hours - observed_hours` is **0 or 1** | no closure in the span. **It was Tuesday.** The new rule was carried but never exercised — the 1-hour gap is the forming bar the adapter drops. |
| `wall_clock_hours - observed_hours` is **~49** (Monday: 90 vs 41) | a weekend was subtracted and the symbol survived. **That is the fix working**, and it is the only shape of evidence that says so. |
| nothing matches the grep at all | the 1d check never ran. Either the market was shut, the cycle was outside `scan_hours_utc`, or an intraday tail was stale and reported first. |

**If forex does not produce anything on Tuesday**, this diagnosis is incomplete and the
fix should not be trusted: check `forex.symbol_skipped` for a timeframe other than `1d`,
because everything here rests on `1d` having been the only one.

### What to watch, and what would be bad

`margin_hours` on the last cycle of a Monday. See §R1 — the number is single digits and
the threshold for acting is `<= 2`.

---

## 1. What was measured, before anything was changed

Nothing here needed a live call. It is the code on `main` plus the spike's recorded
measurements.

### The rule and the failing line

`sentinel/core/forex_cycle.py:184` → `_stale_timeframes` passed
`tail.bid.candles[-1].open_time` into `sentinel/fx/hours.py:212`:

```python
budget = timeframe_to_timedelta(timeframe) * config.max_candle_age_multiplier
return (now - newest_open_time) > budget
```

`forex.max_candle_age_multiplier` is `2` (`config.yaml:613`, model
`sentinel/core/config.py:750`). For `1d` the budget is 1440 min × 2 = **48 hours**.

### What Saxo returns for the newest 1d bar

`journal/M10b_SPIKE.md:533-577`, defect D-k, measured against the live API: the bar
stamped `2026-08-17T00:00:00Z` reconstructs exactly from the `Sun 21:00 → Mon 21:00 UTC`
window. The `00:00:00Z` stamp is a **date label, not the bar's start**.

### The ages, and which comparison fails

`forex.scan_hours_utc` is `[7, 19]` and the orchestrator tests `start <= hour < end`
(`orchestrator.py:563`), so cycles run hourly from 07:00Z to **18:00Z** — the brief's
"19:00Z" is not a cycle. Inside that window the current day's bar is always still
forming, so the newest closed daily bar is yesterday's — except on a Monday, when it is
**Friday's**.

| cycle | newest closed 1d | wall-clock age | budget | verdict |
|---|---|---|---|---|
| Mon 07:00Z | Fri label `08-21 00:00Z` | **79 h** | 48 h | **STALE** |
| Mon 10:36Z — the logged one | Fri label `08-21 00:00Z` | **83 h** | 48 h | **STALE** |
| Mon 18:00Z | Fri label `08-21 00:00Z` | **90 h** | 48 h | **STALE** |
| Tue 07:00Z | Mon label `08-24 00:00Z` | 31 h | 48 h | ok |
| Wed 18:00Z | Tue label `08-18 00:00Z` | 42 h | 48 h | ok |
| Fri 18:00Z | Thu label `08-20 00:00Z` | 42 h | 48 h | ok |

15m/1h/4h are never implicated: they sit on the UTC grid, are dense while the market is
open, and at Monday 07:00Z are 15 min / 1 h / 6 h old against 30 min / 2 h / 8 h budgets.
That is exactly what the live logs said — only `1d` ever appears.

### The defect reproduces against the double, unmodified

The most useful thing measured. `tests/core/saxo_double.py` needed no change to produce
this; only the clock moved:

```
Mon 2026-08-24 07:00Z   scanned=0  {'EURUSD': 'stale candles: 1d', 'GBPUSD': 'stale candles: 1d', 'USDJPY': 'stale candles: 1d'}
Mon 2026-08-24 10:36Z   scanned=0  {'EURUSD': 'stale candles: 1d', 'GBPUSD': 'stale candles: 1d', 'USDJPY': 'stale candles: 1d'}
Mon 2026-08-24 18:00Z   scanned=0  {'EURUSD': 'stale candles: 1d', 'GBPUSD': 'stale candles: 1d', 'USDJPY': 'stale candles: 1d'}
Tue 2026-08-25 07:00Z   scanned=3  {}
Wed 2026-08-19 18:00Z   scanned=3  {}
```

---

## 2. What I pushed back on

### The hypothesis was wrong, in two ways

The brief proposed that the New-York-anchored daily label was being compared against a
UTC-anchored expectation, so "every daily bar reads hours older than it is."

**The sign is backwards.** The label is `00:00Z` on the NY *date*; the bar really starts
at `21:00Z` the previous day. The label is therefore **3 hours newer** than the bar's
real start. Correcting for the anchor makes every daily bar read *older* — 83 h becomes
86 h at the logged cycle — which makes the symptom worse, not better.

**And it changes no verdict.** Measured three ways:

| convention for the Friday bar, read Mon 10:36Z | age | verdict |
|---|---|---|
| D-k date label, `08-21 00:00Z` | 82.6 h | STALE |
| the bar's real start, `08-20 21:00Z` | 85.6 h | STALE |
| the bar's real end, `08-21 21:00Z` | 61.6 h | STALE |

All three fail by 13 to 37 hours. Mid-week all three pass. The anchor moves the number by
±3 hours against a budget that is missed by 31 to 43.

**The real cause is the weekend.** The market is shut ~49 hours a week and the rule
measured wall-clock time. The NY anchor contributes only indirectly: because the daily
grid is NY-anchored, the Sunday-evening session is swallowed into Monday's *still-forming*
bar, so no closed daily bar exists anywhere between Friday and Monday 21:00Z. Under a
hypothetical UTC-midnight grid there would be a Sunday bar — 34.6 h at the logged cycle —
and the symptom would not appear at all.

### "Every cycle since switch-on" is one day, and that day is a Monday

M10d merged Saturday 2026-08-22 11:29 +0300. Saturday is `MARKET_CLOSED`; the market
reopens Sunday 21:00Z, which is outside `scan_hours_utc`. **Monday 2026-08-24 is the
first and only trading day a forex cycle has ever run on** — and Monday is precisely the
day this fires. The sample is one day and the outage is 100% of it.

### Why nothing caught it

`tests/core/saxo_double.py:29` pins `NOW = 2026-08-12 12:05 UTC`, a **Wednesday**, and
every forex test in the repo runs on it. The double's daily grid was already correct —
00:00Z labels, weekdays only, exactly as D-k measured. No fixture was wrong and no mock
was lying. **The untested axis was the day of the week.** That is the M10b boundary
lesson in a dimension nobody had counted as one: not "a producer and its consumer shipped
in different sessions" but "a calendar-dependent rule was only ever asked about one day."

### One boundary error of my own

My first pass computed the last cycle of the day at 19:00Z. `orchestrator.py:563` is
`start <= hour < end`, so it is 18:00Z. Corrected throughout; it moves every "last cycle"
figure by an hour and changes no conclusion.

---

## 3. The fix

**No threshold moved.** `max_candle_age_multiplier` is still `2`.

For `1d` only, age is the number of hourly bars the venue itself served between the daily
bar's label and now — `sentinel/fx/hours.py`, `observed_hours_since` and
`daily_freshness`. The 1h tail is fetched every cycle anyway (1200 bars, ~50 days) and
closed hours are cleanly **absent** from a Saxo response (`M10b_SPIKE.md §6`), so the
hourly tail *is* the venue's trading calendar: weekends, holidays and the 17:00 NY anchor
are all already recorded in it as holes.

Measured, through the real assembler:

| cycle | 1d label | observed | wall clock | margin | verdict |
|---|---|---|---|---|---|
| Mon 2026-08-24 07:00Z | 08-21 | 30 | 79 | 18 | scanned 3 |
| Mon 2026-08-24 10:36Z | 08-21 | 34 | 83 | 14 | scanned 3 |
| Mon 2026-08-24 18:00Z | 08-21 | **41** | 90 | **7** | scanned 3 |
| Tue 2026-08-25 07:00Z | 08-24 | 30 | 31 | 18 | scanned 3 |
| Wed 2026-08-19 18:00Z | 08-18 | 41 | 42 | 7 | scanned 3 |
| Mon 2027-01-25 07:00Z *(winter)* | 01-22 | 30 | 79 | 18 | scanned 3 |
| Mon 2027-01-25 18:00Z *(winter)* | 01-22 | 41 | 90 | 7 | scanned 3 |
| Mon 2026-12-28 07:00Z *(after Christmas Friday)* | 12-24 | 32 | 103 | 16 | scanned 3 |
| Mon 2026-12-28 18:00Z *(after Christmas Friday)* | 12-24 | **43** | 114 | **5** | scanned 3 |
| Tue 2026-08-25 07:00Z, **1d frozen at Friday** | 08-21 | 54 | 103 | **−6** | **SKIPPED** |
| Thu 2026-08-20 12:00Z, **1d frozen at Monday** | 08-17 | 83 | 84 | **−35** | **SKIPPED** |

Winter is identical to summer, row for row. The rail still fires, within one trading day
of a real freeze.

### Why the alternatives are worse

- **Compute the daily bar's real start from its anchor.** Fixes nothing — §2 above: wrong
  direction, no verdict changed. A correct change to a number that is not the problem.
- **Derive the expected anchor from the data.** Already done and already correct:
  `features.daily_anchor` derives 17:00 NY from `zoneinfo` and `assert_alignment`
  cross-checks it against the 4h grid the venue sent. It is not on the staleness path,
  and putting it there lands on the previous bullet's dead end.
- **Exclude `1d` from the check.** Deletes the rail. `prior_day_levels` reads
  `daily.candles[-1]` directly, so a silently stale daily bar puts a wrong "yesterday's
  high" on the card — worse than a skip, because it is wrong rather than absent.
- **Subtract weekends from the elapsed span** (clock-derived, `[Fri 17:00 NY, Sun 17:00
  NY)`, DST from `zoneinfo`). This is the obvious fix and it *works* for the bug as
  reported: Mon 07:00Z → 31 h, Mon 18:00Z → 42 h, frozen-Tuesday → 55 h. **Rejected
  because it does not know about holidays.** The Monday after a closed Friday reads 79 h
  against 48 — the identical outage — and **Christmas Day 2026 and New Year's Day 2027
  are both Fridays**, as is every Good Friday by construction. Counting observed hours
  costs nothing on a holiday because a closed day contributes none: the post-Christmas
  Monday above clears at 32 and 43.
- **A hand-maintained holiday list in `calendar.yaml`.** Would rescue the clock-derived
  rule by reusing a coverage rail that already exists. Not needed, and it would add dates
  to maintain and coverage that can lapse.
- **Raise `max_candle_age_multiplier`.** Not proposed. FOREX.md #21 and #30 are the same
  lesson twice — a rail that cannot fire is worse than an absent one — and the multiplier
  needed to clear a holiday Monday (>4) would let a genuinely frozen daily feed through
  for four days.

### The circularity objection, and why it does not apply

Reading closure from the candle series is normally unsafe: a frozen feed produces the
same absence as a closed market, so a rail that trusts the holes cannot fire. That is
answered here because the **1h tail's own wall-clock rail is checked first, in the same
pass**, on a 2-hour budget that no weekend can excuse. A silently frozen hourly tail is
skipped on its own rule and never becomes the daily rule's denominator. If the 1h tail
does not reach back to the daily label at all, the daily bar is stale by construction —
it is then older than 1200 observed hours.

### Scoping to `1d` is load-bearing, not tidiness

Applying the market-aware rule to 15m/1h/4h would have been a formal no-op today and
would have bought three problems:

1. `tests/fx/test_hours.py:172-176` asserts that a Friday 1h bar read at Sunday midday
   **is** stale — the test that documents *why* `state_at` is consulted first. A
   market-aware rule on the 1h path deletes that reason. Scoped to `1d`, it passes
   unchanged, and its passing is the proof the intraday rails were not touched.
2. In winter, `state_at` reports OPEN from `Sun 21:00Z` (nominal `week_open_hour_utc: 21`)
   while the real market is shut until `22:00Z`. A market-aware intraday rule would call
   a 47-hour-old Friday tail fresh in that hour. Leaving intraday on wall clock keeps that
   shut: the 1h rail fires at 48.5 h against 2 h and the symbol is skipped before `1d` is
   consulted.
3. It is what makes the 1h tail trustworthy as a denominator at all.

---

## 4. DST, in full

D-k measured the anchor at **22:00Z in January and 21:00Z in August** — both are 17:00
America/New_York, and the boundary moves twice a year. The brief is right that a fix
correct today and wrong in November is not a fix.

**The chosen rule has no DST-dependent term.** 15m and 1h bars sit on the UTC grid in
every season (D-k). The holes in the hourly tail move with the venue because the venue
put them there. The arithmetic is a count of bars. There is no timezone table, no anchor
hour, and no constant that is right for forty-four weeks a year — **DST correctness is
inherited from the data rather than derived from a clock.** That is a stronger property
than computing the anchor correctly, because there is nothing to get wrong twice a year.

It is tested in both seasons anyway, and the point is that the tests pass for a structural
reason rather than because a constant happens to be right:

- **Cycle level** — `tests/core/test_forex_daily_staleness.py` runs the Monday cases at
  both ends of the scan window on **2026-08-24** (anchor 21:00Z) and **2027-01-25**
  (anchor 22:00Z). The measured numbers are identical: 30/41 observed, margin 18/7.
- **Unit level** — `tests/fx/test_hours.py` parametrises `daily_freshness` over four
  weekends, including both changeover weekends, where the weekend is **not 48 hours**:

  | weekend | Friday close | Sunday open | length | observed | verdict |
  |---|---|---|---|---|---|
  | 2026-08-21 summer | 21:00Z EDT | 21:00Z EDT | 48 h | 31 | ok |
  | 2027-01-22 winter | 22:00Z EST | 22:00Z EST | 48 h | 31 | ok |
  | **2026-03-06 spring forward** | 22:00Z EST | **21:00Z EDT** | **47 h** | 32 | ok |
  | **2026-10-30 fall back** | 21:00Z EDT | **22:00Z EST** | **49 h** | 30 | ok |

  The US changes at 02:00 local, which falls *inside* the weekend, which is why the two
  changeover weekends are 47 and 49 hours rather than 48. Spring-forward is the tighter
  side — a shorter weekend leaves more market time between the same two calendar instants
  — and at 32 observed hours it is still 16 clear of the budget.

The test helper that builds those grids derives the boundary per date from
`America/New_York` rather than taking a UTC hour, precisely because a single constant
cannot express a weekend whose two ends have different UTC hours.

`tests/core/saxo_double.py` needed the same treatment: `FOUR_HOUR_HOURS` was hardcoded to
August's `(1,5,9,13,17,21)` and `_inside_the_week` to a literal `21`, so a January test
would have tripped `assert_alignment` on a grid the double could not generate. Both are
now derived from 17:00 NY on the bar's own date. At the existing Wednesday `NOW` this
yields the identical tuple — proved below.

---

## 5. The tests, and proof they have teeth

Run against `main`'s source before the fix, the new cycle-level tests fail — 7 of them,
in both seasons and on the holiday:

```
FAILED test_a_monday_after_a_normal_weekend_is_not_stale[summer-7]
FAILED test_a_monday_after_a_normal_weekend_is_not_stale[summer-18]
FAILED test_a_monday_after_a_normal_weekend_is_not_stale[winter-7]
FAILED test_a_monday_after_a_normal_weekend_is_not_stale[winter-18]
FAILED test_the_monday_after_a_friday_holiday_is_not_stale[7]
FAILED test_the_monday_after_a_friday_holiday_is_not_stale[18]
FAILED test_the_margin_is_logged_on_every_daily_check
```

The two "must still be skipped" siblings passed against `main`, as they should — `main`
is strictly stricter. That asymmetry is the point: the fix relaxes exactly the cases that
were wrong and nothing else.

New and changed test files:

- `tests/core/test_forex_daily_staleness.py` — 12 tests. The Monday cases in both
  seasons at both ends of the scan window; the Monday after a Christmas Friday; a daily
  feed frozen at Friday caught on the Tuesday, in both seasons; a mid-week two-day
  freeze; the R1 log line's fields and their arithmetic; the mid-week agreement of the
  two rulers; and a missing hourly tail failing closed.
- `tests/fx/test_hours.py` — the four-weekend DST table, the 79-versus-31 assertion that
  is the whole fix in one line, and the frozen-feed sibling at −7 margin.
- `tests/core/test_market_barren_alert.py` — 11 tests, §6 below.

---

## 6. Why it was silent, and what now speaks

Three pairs, every cycle of a trading day, `status: OK`, `spend_usd: 0`, `/health` green.
Every rail in this system watches for something going wrong. Nothing watched for nothing
going right.

`sentinel/core/alerts.py` gains `AlertKind.MARKET_BARREN`, `BarrenOutcome`,
`consecutive_barren` and `barren_alert`, sitting beside the M8 failure-streak machinery
they mirror. A cycle is **barren** when `status == "OK"`, `symbols_requested > 0`,
`symbols_scanned == 0`, and no entry in the `cycles.skipped` JSONB column carries
`MARKET_CLOSED` or `OUTSIDE_SCAN_HOURS` — that is, *we looked and got nothing*, as
distinct from *the market was shut* or *we chose not to look*. Both of those already have
their own `SkipReason` keys from M8.2, which is what makes this decidable **with no
migration**. Delivered through `UserNotifier` with `barren_market_key(market, at)`, owner
only, exactly as M10d's calendar alert is.

**N = 3, and why.** It is the number ARCHITECTURE.md §2 and §6 already spend on
consecutive cycle failures; a second, adjacent rail with a second, different constant is a
number somebody will misremember. Three cycles is three hours at either market's hourly
cadence, so a Monday-morning outage is on the phone by 10:00Z rather than never, and
three of forex's ~12 daily cycles is not trigger-happy. One or two would fire on a
transient Saxo 5xx or a token refresh spanning two polls — the credential chain has a
one-hour memory by design (§3.1), so short gaps are ordinary operation.

Unlike `cycle_alert` it does **not** re-alert on multiples of the threshold. The condition
persists for the whole day it starts on — thirteen cycles of it on 2026-08-24 — and the
per-UTC-day dedup key is what keeps that from becoming thirteen messages nobody reads.
That is `calendar_coverage_key`'s reasoning, reused rather than reinvented.

**Known limit, stated:** the streak is computed from `cycles` rows and needs three of them,
so the alert arrives on the third barren cycle, not the first. On the outage that
prompted this that would have been 09:00Z rather than "when the owner went looking."

---

## R1 — the margin has a voice

The Monday rail clears by single-digit hours, and D-d found the Sunday pre-open bars to be
unstable between queries (present or absent by anchor and `Count`), which moves the count
by about ±2. **A rail passing by a margin close to its own measurement noise has to be
visible.** So every 1d check emits its arithmetic:

```
{"event": "forex.daily_freshness", "symbol": "EURUSD",
 "newest_1d_label": "2026-08-21T00:00:00Z", "observed_hours": 41,
 "budget_hours": 48, "margin_hours": 7, "wall_clock_hours": 90,
 "hourly_covers_label": true}
```

There was no existing happy-path line to attach these to: `forex_cycle.py` logged four
events and all four fire only on a skip. So this is one new `log.info`, emitted per symbol
per cycle, pass or fail. `wall_clock_hours` rides along deliberately — it is what the old
rule computed, so one line carries both the verdict and the verdict it replaced.

**After next Monday's 18:00Z cycle:**

```bash
docker compose logs --since=2h app | grep forex.daily_freshness | jq -c '{symbol, observed_hours, budget_hours, margin_hours, wall_clock_hours}'
```

**Expected:** `observed_hours` 41-43, `margin_hours` **5-7**, `wall_clock_hours` ~90.
The measured figure against the synthetic venue is 41/7; the real venue also serves
Sunday 19:00Z and 20:00Z bars, which the double does not, so expect 43/5 in production.

| `margin_hours` | reading |
|---|---|
| **≥ 4** | as designed. Nothing to do. |
| **3** | inside the D-d noise band. Watch the next Monday. |
| **≤ 2** | **too thin — act.** One unstable response away from a skip. |
| **< 0** | the rail fired. `forex.symbol_skipped` will be on the same cycle. |

**What to do at ≤ 2, and what not to do.** Not a bigger budget. The right fix is to stop
counting the Sunday pre-open bars D-d says are not reliably there — start the count at
the observed week open (`derive_week_open` already finds it) rather than at the first bar
in the tail. That removes the noise instead of absorbing it, and it costs no detection
latency.

The thinness has a specific cause worth recording: the label `Fri 00:00Z` is neither the
bar's open (`Thu 21:00Z`) nor its close (`Fri 21:00Z`) — it is a fictional instant 21
hours before the bar completed, and every one of those hours is counted as age. Measuring
from the bar's true close would give ~27 hours of margin and was **rejected**: with the
budget unchanged it moves detection of a frozen daily feed from Tuesday to Wednesday,
which is loosening under another name.

---

## Recorded, not fixed

Four things noticed and deliberately left alone.

1. **`/pulse EURUSD` says "The screener triages it every cycle."** Forex has no screener —
   `config.yaml:154` says so in as many words. Crypto's wording on a forex surface.
2. **`forex.calendar_loaded` logs twice a minute.** The tracker reloads the calendar on
   every 60-second tick. It floods the log and made this defect materially harder to find.
3. **Every crypto screener call shows `cache_read_tokens: 0`** — ~$0.024/cycle at full
   input price, no prompt caching. Candidate for the Sept 4 session.
4. **`forex_saxo.py:393` `is_closed` treats the 1d label as a bar start**, so it holds
   Friday's bar "forming" until `Sat 00:00:30Z` — 3 hours after it really closed, 2 in
   winter. It errs late, never early, so no forming bar leaks and nothing is wrong today.
   But that is a consequence of where the label happens to sit rather than a decision, and
   no test guards it. If Saxo ever relabels, this becomes a forming-bar leak silently.
5. **`features.daily_anchor` returns Friday 21:00Z when asked on a Monday** —
   `utc_bounds` is `None` for weekend dates, so the walk-back skips two days. It produces
   the right answer because Sunday 17:00 NY opens both the day and the week, and
   `_open_at_or_after` lands on the same bar Saxo's own daily bar opens from (D-k).
   Correct by coincidence rather than by construction.

---

## Constraints

| # | constraint | how it is met |
|---|---|---|
| 1 | `git diff main -- sentinel/risk/ sentinel/analyst/prompts/` empty | verified empty; neither directory is touched |
| 2 | crypto's decision path untouched | `sentinel/ingestion/` and `sentinel/features/` diff empty. Crypto's rule is `ingestion/staleness.py`, not edited; `fx/hours.py` imports only `timeframe_to_timedelta` from it, unchanged. The changed function has exactly one caller, `forex_cycle._stale_timeframes`, reachable only from the forex branch of the orchestrator. `tests/fx/test_hours.py:172-176` passes unchanged. |
| 3 | `make check` exit 0, no golden moves | green. `tests/fixtures/forex_snapshot_EURUSD.json` regenerated and diffed: the **only** difference is the random `snapshot_id` the generator mints on every run; all 8819 lines of market data are byte-identical. The fixture is not committed changed. |
| 4 | no migration | the barren rail reads `cycles.skipped`, a column M8.2 already added |
| 5 | do not deploy | not deployed |

---

## How to demo

```bash
make check
```

```bash
.venv/bin/python -m pytest tests/core/test_forex_daily_staleness.py tests/core/test_market_barren_alert.py tests/fx/test_hours.py -v
```

To see the defect itself, move the clock and nothing else — `SyntheticSaxo(now=…)` on
Monday 2026-08-24 against `main` skips all three pairs with `stale candles: 1d`, and
against this branch scans all three.
