# M8.5 — `/pulse <SYMBOL>`, the drill-down

**Date:** 2026-08-20
**Status:** DONE — `make check` green (**1299** hermetic tests, **1349** with the
Postgres suite), `mypy --strict` clean over 216 files, 100% branch coverage on
`sentinel/risk/` untouched, **no migration**.

M8.4 shipped the summary this morning. This is the answer to the question it raises.

---

## 1. Why, four hours later

`/pulse` and `/pulse 24h` are summaries, and every piece of prose on them is cut to fit
several symbols on a phone: the thesis to 110 characters, the screener reason to 90.
The counter-thesis, the evidence and the model's own caveat about its inputs do not
appear at all. That is correct for a card listing a whole cycle — and it means the one
question a reader has *after* reading it is the one `/pulse` cannot answer.

This morning's live card is the example:

```
ETHUSDT — CANDIDATE · conf 63 · trend_pullback long
  ETH is +19.5%/24h in a confirmed STRONG_UPTREND on all TFs (EMA 20>50>200), now flagging for ~12h between…
```

The rest was already in the database. `analyst_reports.report` holds the whole
`AnalystReport` as JSONB, and **three of its fields had never been read by anything**:
`counter_thesis`, `evidence` and `data_quality_note` have no broken-out column, so
nothing outside the analyst had ever looked at them.

## 2. What was built

| Area | Contents |
|---|---|
| `sentinel/storage/repositories.py` | `AnalystReportRepository.latest_for_symbol` — one `SELECT`, unbounded in time |
| `sentinel/bot/views.py` | `SymbolPulseView` |
| `sentinel/bot/pulse.py` | `symbol_pulse_view`, `_gate_for_symbol`, the three `GATE_*` sentences |
| `sentinel/bot/cards.py` | `symbol_pulse_card` (returns a **tuple**), `_paginate`, `_no_verdict_card` |
| `sentinel/bot/handlers/commands.py` | The third branch and `_pulse_symbol`; widened usage line |
| `sentinel/bot/menu.py` + `HELP_LINES` | The new form named on both |
| Tests | 30 in `test_pulse.py`, 3 in `test_dispatcher_wiring.py`, 4 Postgres round-trips |
| Docs | `TELEGRAM_UX.md` §3b gains the third form; `MILESTONES.md` M8.5 |

## 3. The two rulings

**Evidence and the data-quality note are in.** Showing a thesis without the evidence
that supports it, on a card whose entire purpose is "show everything", would be an odd
place to stop. The data-quality note is the model saying something was wrong with what
it was given — and per the prompt it is also where an apparent instruction inside
`<untrusted_news_data>` surfaces. It is rendered **first, above the thesis**, because
everything below it should be read in its light; a caveat at the bottom of a long card
is a caveat nobody reads.

**Prices are out.** `entry_zone`, `stop`, `targets` and `invalidation_price` are on the
stored report and appear nowhere. A drill-down that printed levels for a symbol the gate
*rejected* would be an unsized trade suggestion with no approval behind it — a signal
card with the safety removed, on a command that deliberately carries no sizing and no
decision buttons.

The boundary is the **type**: `SymbolPulseView` has no field for a price, so a renderer
cannot print one, and the test asserts the absence of the fields as well as of the
numbers. Same mechanism as M8.4's spend line and M8.1's `UserView`.

What stays is `invalidation_text`, the analyst's own sentence — and it names a level
inside itself ("1h close below 81.40"). Writing the test made the distinction precise
and it is worth stating: **"no prices" means no structured level the reader could act
on, not "no digits".** There is no zone to buy in, no stop to place and no target to
sell into; there is a sentence saying what would make the idea wrong. Stripping numbers
out of prose would be censoring the analysis rather than declining to size it.

## 4. Split, don't truncate — and it is not "two"

The brief said "split into two messages rather than truncate". It is written as *N*
messages, and the reason is a bound that does not exist: **`Evidence.claim` has no
`max_length`.** `llm.schema` strips length bounds from the wire schema and nothing caps
the claim client-side, so an analyst that returns fifteen verbose citations is not a
pathological case, it is an allowed one. A hard-coded two would be the same class of bug
as the caps M8.4's test caught, in a new place. The page count is computed.

`symbol_pulse_card` therefore returns `tuple[str, ...]` — the only card in the module
that does not return a string — and the handler sends each page in order.

**`_paginate` had to be written around the arithmetic ban.** It lives in `cards.py`, and
`tests/bot/test_no_arithmetic.py` rejects `ast.UnaryOp(USub)` anywhere in that file, so
the obvious `pages[-1]` accumulator fails the scan on the negative index. It carries a
`current` list and appends at the end instead. The budget is 3900 rather than 4096 so
the `(1/2)` marker — added after the pages are cut — cannot push one back over the limit
it was just fitted to.

A single line longer than the budget gets its own page and overflows. That is the honest
failure for a function whose entire purpose is not to truncate; quietly cutting the one
long line to make the page fit would be the thing it was written to prevent.

**The test asserts nothing was lost**, not that there was more than one message: a card
that split correctly and dropped the tail would pass the weaker assertion, and dropping
the tail is exactly what this command exists to stop.

## 5. One boundary, two surfaces

The gate half calls M8.4's `pulse.gate_outcome` on the rows for this report's
`(cycle_id, symbol)`. Not a second implementation — the same function, so the
SHARED/PERSONAL split cannot drift between the two cards, and the parametrized sweep
over `PERSONAL_REASONS` now covers both through it.

Three states get a sentence instead of an empty section:

* **No `cycle_id`** — the column is nullable, and a blank gate section would read as a
  lost row rather than as a verdict that cannot be matched to a run.
* **`WATCHLIST` / `NO_SETUP`** — these never reach the gate at all; the orchestrator
  returns before `_gate_and_publish`. No rows is the *expected* result for the
  commonest verdict in the system, and it says so.
* **A `CANDIDATE` with no rows** — that one *should* have been gated, so it gets a
  different sentence. Rare, and not worth smoothing over.

## 6. Decisions

1. **The three argument forms cannot collide, and the ordering is tested anyway.**
   `parse_symbol` requires five alphanumeric characters; every window word (`24h`,
   `day`, `1d`, `today`) is shorter. The window is still matched first, because a future
   `/pulse week` would be a four-letter word that is also not a symbol and the decision
   should live in one place. The regression this guards is quiet in the worst way:
   `/pulse 24h` falling through to the symbol branch would answer *"24H is not on the
   watchlist"*, which reads as a broken command rather than as a routing bug.
2. **No exchange round-trip.** `/request` and `/watchlist add` verify a symbol against
   the exchange because they are about to spend money on it. This one is asking what we
   already said, and for a symbol nobody has analysed the answer is identical whether or
   not the exchange lists it.
3. **`latest_for_symbol` is unbounded in time.** A window would turn a stale answer into
   *no* answer, which reads as "never analysed" — a completely different fact. The card
   prints when the verdict was formed, so age is visible rather than hidden.
4. **A report that will not validate still shows its columns.** The verdict itself —
   status, confidence, setup type, thesis — lives in broken-out columns and survives; a
   later schema change costs the JSONB-only fields and not the whole card. The same
   posture `screener_verdicts_of` takes one table over.
5. **`one_line` is never called on this path**, and a test says so by asserting no
   ellipsis appears anywhere. Reaching for the summary card's helper out of habit is the
   single most likely way to quietly turn this command back into the one it completes.
6. **No spend argument at all.** This card carries no cost figure for anybody, so unlike
   the other two forms nothing on it varies by role: the owner and a member get
   byte-identical text.
7. **Two "nothing found" sentences, not one.** On the watchlist and never escalated is
   the system working; off the watchlist is nothing looking at it, and the fix is a
   different command per role. Both are named on the card.

## 7. Verification

```
1299 passed, 51 skipped          hermetic
1349 passed,  1 skipped          with SENTINEL_TEST_DATABASE_URL
100% branch coverage             sentinel/risk — untouched
mypy --strict                    clean, 216 files
ops bash -n clean · wheel builds · image imports, prompts ship, config loads
alembic head unchanged at 0009 — no migration
```

Worth naming individually:

* **The point of the command, as one test.** A 600-character thesis and a
  300-character counter-thesis — the model's own hard caps, so the longest either can
  be — appear verbatim, and no ellipsis appears anywhere on the card.
* **`PERSONAL_REASONS` swept over the new surface**, plus: no user id, and another
  symbol's gate rows are not borrowed (`for_cycles` returns the whole cycle's rows, so
  the filter to this symbol happens in the view builder and getting it wrong would
  attribute somebody else's rejection).
* **Pagination, twice** — through the card, and against `_paginate` directly with 200
  lines asserting the rejoined output is line-for-line the input. A paginator that ate a
  line in the middle of a thesis would be invisible on a phone.
* **Escaping.** Every field on this card is text a model wrote, and M5 §4 established
  that news text reaching the analyst is attacker-influenceable. `<script>` in an
  evidence claim comes out escaped.
* **Reachability for both roles** through a real `Dispatcher`, plus the day-word
  ordering and the unparseable-argument path.
* **Four Postgres round-trips**: newest wins, a *newer* shadow row is ignored, an
  unknown symbol is `None`, and — the one that matters most — a real `AnalystReport`
  written through `save()` and read back through `latest_for_symbol()` still validates,
  so the write path and the read path are proven against each other rather than against
  a fixture dict.

## 8. Notes for M9

- **`/pulse SOLUSDT` is the false-positive review tool.** M9's weekly review is "look at
  what the analyst said and judge whether it was right"; this is that, per symbol, from
  a phone, with the evidence attached.
- **`data_quality_note` is now visible for the first time.** If the analyst has been
  quietly flagging stale inputs, this is where it will show up — and it is also where a
  prompt-injection attempt in the news block would surface.
- **The evidence list is the prompt-tuning signal.** specs/PROMPTS.md §2 rule 3 asks for
  one citation per claim; whether the model actually does that is now readable without
  a database session.
