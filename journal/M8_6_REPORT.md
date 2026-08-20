# M8.6 — `/journal` and `/snapshot`

**Date:** 2026-08-20
**Status:** DONE locally — `make check` green (**1399** hermetic tests, **1453** with
the Postgres suite), `mypy --strict` clean over 221 files, 100% branch coverage on
`sentinel/risk/`, and **no migration**: `alembic head` is unchanged at `0009`.

Two gaps either side of what M8.1–M8.5 built. Nobody could get their own history
*out* of this system, and the half of the pipeline with no AI in it had never been
readable from a phone.

---

## 1. What was built

| Area | Contents |
|---|---|
| `sentinel/stats/journal.py` | New. `JournalRow`, `JournalBook`, `JournalPopulation`, `build_journal` — every number on the sheet, pure and `Decimal` |
| `sentinel/bot/export.py` | New. `journal_workbook` (openpyxl into a `BytesIO`), `journal_filename`, the column table, the Legend |
| `sentinel/bot/snapshot.py` | New. `snapshot_view`, the four block validators, the OI 24h derivation, the display helpers |
| `sentinel/bot/views.py` | `SnapshotView` + six sub-views |
| `sentinel/bot/cards.py` | `snapshot_card`, `journal_empty_card`, `journal_caption`, `_pages`; `help_card()` paginated; two `HELP_LINES` paragraphs |
| `sentinel/bot/handlers/commands.py` | `/journal [30d\|90d\|all]` and `/snapshot <SYMBOL>` on `commands_router` |
| `sentinel/bot/outbound.py` | `SupportsDocuments`, composed into `SupportsBot` |
| `sentinel/bot/menu.py` | Both on `MEMBER_COMMANDS`; the owner inherits them through `commands_for` |
| `sentinel/risk/accounting.py` | `avg_exit_price` — the mirror of `avg_fill_price`, for the exit column |
| `sentinel/storage/repositories.py` | `SignalRepository.journal_since` |
| `pyproject.toml` | `openpyxl>=3.1` (owner-approved) + the mypy override |
| Tests | `tests/bot/test_journal.py` (42), `tests/bot/test_snapshot.py` (26), 9 in `test_dispatcher_wiring.py`, 4 Postgres round-trips, 4 in `test_accounting.py`, plus `test_help.py` and `test_menu.py` |
| Docs | `TELEGRAM_UX.md` §3c and §3d + two rows in §3's table; `MILESTONES.md` M8.6 |

## 2. `/journal`: three definitions, each a place it could have disagreed with itself

The interesting work was not the spreadsheet. It was that three figures on it already
have meanings elsewhere in this system, and getting any of them subtly different would
have produced two surfaces telling one person two stories about the same trade.

**1R is `planned_risk_eur`.** Every R figure in the system is already on that basis —
`tracker/loop.py` computes `realized_eur = r * plan.planned_risk_eur` — so the
`Risk EUR (1R)` column is that and not `plan.risk_eur`, the step-floored actual. The
two differ by cents. Against the wrong basis, `PnL R x Risk EUR` and `PnL EUR` would
diverge by a **bias that grows with the figure**; against the right one they differ
only by R's own 2dp rounding. R stays at two decimals because that is what every card
prints, and widening it here would make the journal disagree with the card instead.
The Legend states that the euro columns are the exact ones.

**Net subtracts costs from the numerator only**, and deliberately not the way
`risk/costs.net_rr_multiples` does it — that function also grows the denominator,
because it prices a *prospective* trade where the cost is money additionally at risk.
Here the trade is over, R is a fixed unit of account, and a running balance has to add
up. The divergence is written into both docstrings, because whoever reads them
together will otherwise conclude one is a bug.

**A win is counted on gross R and the balance is net.** `stats/compute.py` defines a
win as realized R > 0 on the gross figure, so the last row of the Real sheet equals
what `/stats` reports for the same window. A balance, though, is money, and costs
really do come out of it. The consequence is real and is asserted: a trade that won
gross and lost after fees raises the win rate and lowers the running balance. That is
already true of `/stats`; the Legend says it out loud.

**Open signals are rows with the running columns blank** (your ruling). The reasoning
is worth keeping: had an open row participated, every earlier row's running balance
would silently renumber on the day that trade resolved and landed somewhere else in
history. A record that reorders itself is not a record. Blank means "not yet part of
this"; a zero would claim the trade contributed nothing. `test_an_open_signal_changes_
no_earlier_running_balance` is the assertion that states it directly.

**One sheet per population**, because a cumulative series that walked from a trade you
took into one you skipped would be a number nothing in this system endorses. The
`Decided` column you asked for earns its width in exactly two places — Watching and
Skipped share a population, and a rehearsal has a population and no decision at all —
and both are tested.

## 3. The scoping proof, and why it is three assertions rather than one

`/journal` is the most per-user surface in the bot and the only one whose output leaves
the chat as a **document** — forwardable, saveable, outliving its caption. Three
mechanisms carry the boundary, none of them a filter somebody has to remember:

1. `journal_since(since, *, user_id)` — keyword-only, no default, exactly as
   `resolved_since` is. M8.1 §4's third hole was a read that silently stopped meaning
   "mine" the moment a second person existed.
2. `JournalRow` has **no field** that could hold an id. The meta-test asserts the
   complete field set rather than pattern-matching names, so a new column fails the
   test and forces somebody to look — `confidence` and `decided` both contain the
   letters "id", which is how the first draft of that test found itself green for the
   wrong reason.
3. The file is addressed to the caller's own id, and the **filename carries no id and
   no name**.

The proof runs three ways: the books hold no B row, the workbook read back through
openpyxl holds no B cell, and the raw zip's XML contains no B string. The third is not
redundant — a value can reach the artefact without reaching a cell a reader would see
— and a fourth test deliberately builds a leaking workbook and asserts all three
checks catch it, because a guard that cannot fail is not a guard.

## 4. `/snapshot`: the command whose whole content is what could not be measured

Everything it shows has been on the `market_snapshots` row since M1 and readable only
over SSH. The build was mostly about absence, which is the half fixtures are worst at:

* every optional block missing, one at a time, **named** rather than dropped or
  zero-filled — a funding rate that was not fetched is not a funding rate of zero;
* `RegimeBasis` shown beside the regime, so a `REDUCED` read (a 1d tail with no
  EMA200) can never pass for a full one;
* a stored block that no longer validates costing that section and not the card;
* the two distinct "nothing stored" answers `/pulse <SYMBOL>` already separates.

It takes no `Actor` at all. There is nothing on the card that could vary by caller, and
a parameter the handler does not receive is a boundary it cannot cross — the cheapest
version of M8.4 §5's mechanism.

One derivation lives in the view builder: open interest's 24h change. `DerivContext`
stores the series and no delta because nothing upstream ever needed one, and computing
it here rather than adding it to what the pipeline *writes* keeps this milestone
read-only. Two points are the minimum; one point is a reading, not a change.

## 5. Decisions

1. **openpyxl over XlsxWriter** (your call, and approved under CLAUDE.md's dependency
   rule): 250KB wheel, ~1.3MB unpacked, pure Python, no native extension to break the
   image build. Never used through pandas, which would put a `Decimal` column through
   a numpy dtype first.
2. **`/journal` defaults to `all`** where `/stats` defaults to 30d. A statistic is a
   report about a recent period; an export is an archive.
3. **The window is on `created_at`**, where `resolved_since` windows on `closed_at`.
   Otherwise a signal issued inside the window and still running would fall out of its
   own journal.
4. **An unrecognised window gets usage, not a silent default** — M8.4's ruling for
   `/pulse 7d`. Somebody who asked for a week and received two years would read the
   file as a week's worth.
5. **An empty journal is a sentence, not an empty file.** A workbook of nothing but
   headers is indistinguishable from a broken export, and the reader would open it to
   find out which.
6. **Euro columns on the Real sheet only.** Elsewhere nobody placed the trade, so a
   euro figure would assert money that was never at risk. The R columns stay, which is
   the point of resolving a hypothetical.
7. **`avg_exit_price` went into `sentinel/risk/`**, not the bot. A position closed
   across TP1, TP2 and a stop has one honest exit price and it is the weighted one;
   averaging the legs in a renderer would be a second, untested implementation of the
   basis the row's own R figure already uses. Four tests, and `sentinel/risk/` is still
   at 100% branch coverage.
8. **`/help` splits rather than shortens.** It sat at 3,859 of 4,096 characters and two
   more commands needed ~470. M8.5 §4 already settled the choice for this codebase, and
   the `_paginate` it settled it with was already in `cards.py`; it is now shared by
   both callers through `_pages`.
9. **`SupportsDocuments` is composed, not welded on.** The publisher, notifier and
   alerter depend on `SupportsSending` and none of them will ever send a file.

## 6. What the live database found — four legibility bugs, none of them a fixture's fault

M8.4 §9a and M8.5 §7a established the practice: render against a real database and read
the result as a stranger would. It paid again, four times, and every one of them is the
same shape — a fixture proves a card holds the right facts, and only real data shows
how it reads.

**1. Eighteen trailing zeros on a price.** `market_snapshots.last_price` is
`Numeric(38, 18)`, so a price the fixtures hold as `64100.0` comes back out of Postgres
as `64100.000000000000000000` and rendered as `64100.000`. Reaching for `normalize()`
alone gives `6.41E+4`, which is worse — the exact trap `risk/rounding.percent` already
documents. Fixed with normalize-then-requantize-if-integral, and caught by the
**Postgres round-trip**, not by any of the 26 hermetic snapshot tests.

**2. `(≈ n/a USDT)`.** `open_interest_value` is optional on `DerivContext` and Binance
frequently omits it, so a live ADAUSDT row read `Open interest 7,696,615 in base units
(≈ n/a USDT)`. No parenthetical is a better line than an empty one.

**3. Eight significant digits is noise, not precision.** `EMA20 6.3501787` beside
`last 6.327` on a real AVAXUSDT card is four digits past that symbol's tick. Six
significant digits keeps every EMA at or past the tick for both ends of the range this
system trades — `63820.6` for BTC, `6.35018` for AVAX — and was chosen by looking at
the two, not in the abstract.

**4. An empty `Decided` cell.** openpyxl stores `""` as an empty cell, and an empty
cell reads as *missing data* — the opposite of what that cell means, which is that
nobody answered. It is `—` now. Found on signal #42, the one real row in the local
database, which is a dry-run signal and therefore has no decision at all.

Two further things the real data confirmed rather than corrected: the caption reported
`Real (Taken) 0 · Hypothetical 0 · Dry run 1` honestly, and the Legend listed every
population's name twice (a counts block and a notes block) — merged into one row per
sheet, which is a legibility fix found by reading the sheet rather than by a test.

## 7. Verification

```
1399 passed, 55 skipped          hermetic
1453 passed,  1 skipped          with SENTINEL_TEST_DATABASE_URL
100% branch coverage             sentinel/risk — one function added, four tests with it
mypy --strict                    clean, 221 files
ops bash -n clean · wheel builds · image imports, prompts ship, config loads
alembic head unchanged at 0009 — no migration, no new table, no new column
```

Worth naming individually:

* **The scoping proof and its proof-of-teeth**, described in §3.
* **Every figure round-trips through the XLSX unchanged.** XLSX numbers are IEEE
  doubles, so `Decimal` ends at the file boundary whatever writes it — openpyxl renders
  through `"%.16g"`, which is why the raw XML for `83.10` reads `83.09999999999999`.
  That text parses back to the same double, and the money columns carry a 2dp format,
  so the assertion is on the round trip rather than on the bytes: the bytes may be
  ugly, the value may not move.
* **The four journal populations map onto `/stats`' three**, asserted over the whole
  `(decision x dry_run)` cartesian product. A split is fine; a disagreement would mean
  a signal counted REAL on one surface and HYPOTHETICAL on the other.
* **Reachability for both roles** against a real `Dispatcher` for `/journal`,
  `/journal 30d`, `/snapshot SOLUSDT` and the bare `/snapshot`, plus silence for a
  caller with no standing — asserted on outbound calls, since a `DROP` returns `None`
  and `feed_update` hands that back verbatim. The existing member meta-test forced
  these the moment the menu entries landed, which is the mechanism working.
* **Four Postgres round-trips**: `journal_since` scoped and windowed, open signals kept
  where `resolved_since` drops them, a real `MarketSnapshot` saved and read back into
  the card it will be rendered from, and `latest_for_symbol`'s newest-wins selection.
* **A maximal `/snapshot` card fits one Telegram message**, filled to the level cap
  with every block present — 4096 is a refusal, not a truncation.

## 8. Deviations from spec

None. Both commands are **additions**, recorded as new dated sections
(`TELEGRAM_UX.md` §3c, §3d) in the manner of §3a and §3b, plus two rows in §3's table.
The one behavioural change to something that already existed is `/help` splitting
across messages, which is noted in §3d under M8.5's standing ruling.

Three things were added beyond the column list you gave, each because a figure nobody
can check is a figure nobody should trust: `Costs EUR`, `PnL R (gross)` and
`PnL EUR (gross)`, so `gross − costs = net` reconciles on the sheet and the gross
figures tie back to `/stats`.

## 9. What is left for you

1. **Deploy** — `docs/DEPLOY.md` §12. This adds a runtime dependency, so the image
   rebuilds; `check-image` already proves the app imports with `openpyxl` inside it.
   No migration: the schema stays at `0009`.
2. **The live Telegram round trip**, which cannot be verified from here — one bot
   token, one long-polling client, and the server holds it (journal/M8_REPORT.md §9a):
   - `/journal`, and open the file. Check the Legend reads like something you would
     hand someone else.
   - `/journal 30d`, and `/journal 7d` to see the usage line.
   - `/snapshot BTCUSDT`, and hold it beside `/pulse BTCUSDT` — that pairing is the
     whole point of the two commands existing.
   - Type `/` and confirm both are on the menu.
   - `/help` now arrives as two messages. Read it as if you had not written it.
   - If you still have a member account to hand: `/snapshot` from it (byte-identical
     text) and `/journal` from it (only that account's rows).
3. **Sending a document is a new Telegram API call for this bot.** Everything else it
   sends is a message, an album or a menu. If `send_document` is going to fail on
   something environmental — a file-size limit, a permission — this is the first
   milestone where that can happen.

## 10. Notes for M9

- **`/journal` is the weekly-review instrument.** M9's exit criteria are "≥25 tracked
  signals, zero sizing bugs, a first prompt-version comparison" — the first two are a
  sort and a scan of the Real sheet, and the third is `/stats`' breakdown beside it.
- **The Hypothetical sheet is the one to read first.** It is what skipping cost or
  saved, per signal, which is the question `/stats` can only answer in aggregate.
- **`Decided` versus `Population` is a data-quality signal.** A growing `Undecided`
  sheet means cards are arriving and not being answered, which quietly corrupts every
  real statistic downstream — and until now nothing surfaced it.
- **`/snapshot` is where a suspicious analyst claim gets checked.** M9's false-positive
  review is "look at what the analyst said and judge whether it was right"; `/pulse
  <SYMBOL>` gives the claim and this gives the evidence it was drawing on.
- **Adding a `JournalPopulation` now fails a test** until it has a label, a place in
  the sheet order and a mapping onto `/stats`' populations. That is the intended cost.
