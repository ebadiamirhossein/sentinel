# M11p — Persian summaries on cards

Branch `m11p-persian-summary` off `main` (01c3999). **Not deployed.**

## State at handoff

A 🇮🇷 فارسی button on crypto signal cards, forex signal cards and `/pulse SYMBOL`
verdicts. Press it and a short spoken-Persian explanation of that card arrives as a
reply underneath it. The card is not edited and the button does not disappear.

- `make check` **exit 0**. 2,212 passed / 74 skipped without a scratch database;
  **2,285 passed / 1 skipped** with `SENTINEL_TEST_DATABASE_URL` set.
- `git diff main -- sentinel/risk/ fable_v1.md fable_forex_v1.md screener_v1.md
  screener_v2.md` — **empty**.
- `git diff --stat main -- sentinel/analyst/providers/ sentinel/tracker/ sentinel/stats/
  sentinel/screener/ sentinel/risk/ sentinel/fx/` — **empty**. The analyst path, the
  gate, sizing, the tracker, `/journal`, `/stats` and the three populations are
  untouched, and none of them can see this feature.
- **No golden text file moved.** One was added: `card_shared.txt`. Details in §3.
- Migration `0012_persian_summaries` adds **one table** and nothing else. The
  upgrade → downgrade → upgrade round trip was run against the real Postgres suite;
  the transcript is pasted in §4, with `\d signals` diffed across it.
- **Two rails**, both fail-closed: every number in the output must be on the card, and
  the verdict line must agree with the card's verdict (§6, §7).
- Three real calls measured: **$0.012188 a press** cold, $0.007071 warm. Both spend rails
  set *from* those figures. Full output in §5.

**Nothing is deployed.** When the owner next deploys, `alembic upgrade head` runs at
container start and creates the table.

### The two things to know before reading further

1. **The brief as written would have shown one user another user's position size, in
   the language he trusts most.** "The owner and an approved member reading the same
   signal read the same Persian words" is impossible for a rewrite of a whole card,
   because `signals` is one row *per approved user* and each card carries that user's
   own capital, size, notional, margin and cost block. Settled before building: the
   Persian text covers the **analysis only** and never the sizing.
2. **Sizing is not a scaling, and the differential test found it in the first hour.**
   At €200 the risk engine returns a genuinely *different plan* from the one it returns
   at €10,000 — on the golden BTCUSDT setup, three ladder rungs become two, and TP1 net
   RR moves 1.75R → 1.50R. So even the analysis-only text is not universally shareable,
   and the cache is keyed on a hash of the exact text rather than on an assumption. §2
   has the numbers.

   **This is bigger than this milestone and is written where the Sept 3 review will
   read it**, at the owner's instruction: HANDOFF.md §4 item 15, and a dated block on
   `docs/MILESTONES.md`'s M9 section with two questions added to that review. The
   owner's live capital is €200 and every golden sizes at €10,000, so the measured
   sample comes from a region of the engine's behaviour no golden has ever pinned —
   and `min_rr_tp1 = 1.5` sits exactly where the €200 figure landed.

3. **The verdict rail exists now (owner requirement H1)**, because the numbers rail is
   structurally blind to a WATCHLIST rewritten as an encouraging card. §7, including
   the proof of teeth against real model output.

---

## 1. What I pushed back on

### 1a. §1 and §F contradict each other

Constraint 1 mandates one add-a-table migration with a working downgrade, round-tripped
against real Postgres and pasted. §D says "no storage this milestone". §F is titled
"STORAGE — build it now" and ends "Do not add it now."

**Owner ruling: ship the table now.** Nothing is deployed, so the migration runs when he
next chooses to deploy. Had the other reading won, Constraint 1's migration test would
have had nothing to test.

### 1b. "The same Persian words for both users" — the important one

> **My brief would have shown one user another user's position size in the language he
> trusts most.**

That is the owner's own sentence and it is the accurate description. `signals.user_id`
([models.py:592](../sentinel/storage/models.py)) — one shared `AnalystReport` produces
one row per approved user, each sized against that user's own capital. The card carries
`capital €…`, `risk … = €…`, per-rung quantities and euro notionals, `💶 Notional /
Margin / Leverage`, the cost block and `⚖️ Actual risk`. A Persian rewrite of that card,
shared between two people, is a disclosure.

**Settled: the Persian text covers the analysis only.** Verdict, what is good, why not
now, the entry condition, what invalidates it. Never capital or size. The owner's two
supplied examples contain no sizing at all, which is a good sign the requirement was
always about the analysis and the sharing sentence was the slip.

The subtraction happens **in the renderer** — `signal_card(..., shared_only=True)` and
its forex twin — not by parsing the rendered string. String surgery on our own output is
the kind of cleverness this codebase punishes; a flag on the one function that already
knows which values are private is the honest version. It defaults to `False`, so the
card the owner reads has not moved a byte, and `card.txt` proves it.

### 1c. "Keyed by signal id" is the wrong key, twice over

Signal ids are per-user (1b), and a `/pulse SYMBOL` verdict card has no signal id at
all. The table is keyed on **`sha256` of the exact text sent to the model**, with
`signal_id` and `analyst_report_id` kept as nullable indexed columns for M13's one-hop
join. That delivers what the requirement asked for — identical input, identical output,
one paid call — for both card kinds and across users, with no special case.

### 1d. "/pulse analyst verdict cards" is read as `/pulse SYMBOL` only

Not the `/pulse` cycle summary or `/pulse 24h`. Those are lists of many symbols; a
Persian rewrite of a list is not the product described, and the cycle card carries an
owner-only spend line. `/pulse SYMBOL` is the drill-down that renders one full verdict,
and its handler's own docstring records that it is byte-identical for the owner and for
a member — so it needs no `shared_only` equivalent.

### 1e. Found in passing, not fixed

`sentinel/bot/forex_cards.py:6-8` and `tests/bot/test_forex_cards.py:12` both state that
the forex renderer is covered by `test_no_arithmetic.py`. **It is not** —
`RENDERING_MODULES` names only `cards.py` and `formatting.py`. Two docstrings assert a
guard that does not exist over the renderer that draws real forex cards today. It is a
one-line change plus a fixture, out of scope here, and it belongs on M9's hygiene list.

---

## 2. The measurement that changed the design

The plan called for a differential test: size the same analysis at €200 and at €10,000,
render both shared forms, assert they match. **It failed on the first run**, and not for
a presentational reason.

On the golden BTCUSDT setup:

| | €10,000 | €200 |
|---|---|---|
| ladder rungs | 3 | **2** |
| risk weights | 40 / 35 / 25 | 53.33 / 46.67 |
| average fill | 63961.5 | 64062.5 |
| stop distance | -0.86% | **-0.97%** |
| TP1 net RR | 1.75R | **1.50R** |

`min_notional = max(exchange_min, 20 USDT)`, the quantity step and the leverage cap all
bite in **absolute** terms, so a small account cannot fund the third rung at all and
pays a different share of its risk budget in costs. Two users are not reading one plan
rendered two ways. They are reading two plans.

On SOLUSDT the divergence is at its smallest and is therefore the sharper case: every
price is identical and only the cost-derived net R multiples move, by 0.01R. Still a
difference the owner would read.

**What this changes:** nothing structural, because the hash key already handles it —
two users whose plans coincide share one row and one paid call; two whose plans differ
get two rows, which is correct. What it changes is the *claim*. "Both users read the
same Persian words" is true exactly when their plans coincide, and the system no longer
assumes that. `test_two_capitals_do_not_share_a_shared_form_and_this_is_not_a_bug` pins
it so the next reader meets it as a documented fact rather than a surprise.

### Why the adversarial test is the load-bearing one (owner requirement G1)

A purely differential test — render twice, assert the two match — cannot detect a
`shared_only` that does nothing: two renders of a dead flag match perfectly, as two
identical **full** cards, with every per-user number in front of the assertion. That is
HANDOFF §4 item 10's shape exactly.

So the guard that actually has teeth names the forbidden vocabulary and asserts its
**absence**: `Notional`, `Margin`, `Leverage`, `Actual risk`, `Liq. buffer`, `Costs`,
`Pip value`, `Your call`, `Signal #`, `capital`, `risk budget`, `planned` — and no `€`
anywhere, which catches a new money line whose label is not yet on the list. Run over
both renderers. A third test asserts every line the shared form *keeps* is byte-identical
to its full-card counterpart, so the flag can drop lines and shorten the two it declares
and can never alter one it keeps.

---

## 3. The goldens — exactly what moved

**No golden text file moved. Not one byte.**

```
$ git status --short tests/fixtures/golden_cycle/
?? tests/fixtures/golden_cycle/card_shared.txt
```

`card.txt`, `card_multi.txt`, all nine `surfaces/`, all nine `surfaces_multi/`, both gate
JSONs, all three feature and chart sets — untouched and passing.

**Why the button moved nothing.** The goldens pin card *text*; no golden stores an
`InlineKeyboardMarkup`, and `/pulse` carried no `reply_markup` at all before this. The
brief anticipated that goldens would move and asked which; the honest answer is that the
markup was never pinned by one.

### What did move: three keyboard-shape assertions in the bot suite

| where | before | after | why |
|---|---|---|---|
| `test_callbacks.py` — unfilled signal | `len(rows) == 1` | `== 2` | every rebuilt keyboard now ends with the 🇮🇷 فارسی row |
| `test_callbacks.py` — filled signal | `len(rows) == 2` | `== 3` | same, plus §2's manage row |
| the manage-row assertion itself | by index `[1]` | unchanged | still asserted by position; the claim the test is about did not move |

`decision_keyboard` and `decision_keyboard_with_manage` have a **zero-line diff**, and
`test_tracker_updates.py:299`'s `len(...) == 2` is still green — that is why the Persian
row is composed at the three call sites instead of inside them.

### The golden that was added, and which branches it reaches

`card_shared.txt` is `signal_card(shared_only=True)` on the golden BTCUSDT decision,
written by `tests/fixtures/generate_goldens_m11p.py`, which carries the same hard
write-allowlist `generate_goldens_m10c.py` uses: `card.txt` and `surfaces/` are
unreachable from it by construction.

Per HANDOFF §4 item 10 — a golden pins the case it was built from and nothing else.
BTCUSDT at €10,000 reaches the three-rung ladder and the `shared_only` branch of every
line the flag touches. It does **not** reach the two-rung ladder a small account
produces, nor the forex renderer's own flag. Both are covered by
`tests/bot/test_shared_card.py`, which asserts on structure rather than on bytes —
deliberately, because a byte golden of a card that changes with capital would pin one
account size and call it the truth.

`test_the_shared_golden_is_not_the_full_one` is its proof of teeth: a `shared_only` that
did nothing would write `card.txt`'s bytes into `card_shared.txt` and both files would
pass.

---

## 4. The migration

`0012_persian_summaries`, `down_revision = 0011_forex_spine`. **One `create_table`, three
`create_index`, and nothing else.** No `ALTER`, no column on `signals`, no index on any
existing table, no backfill, no `ALTER TYPE` (`llm_calls.kind` is a `varchar(16)` and
`PERSIAN_SUMMARY` is fifteen characters).

### What the table holds, and why each column is there

| column | why |
|---|---|
| `id` uuid PK | |
| `input_sha256` char(64) **UNIQUE** | the cache key. Identical input → identical output, one paid call, for both card kinds and across users. See 1c for why this and not the signal id. |
| `source_kind` varchar(16) | `signal` \| `pulse_verdict` — says which id column below is populated, so a reader never has to infer it from a null |
| `signal_id` uuid NULL, indexed | M13's one-hop join for signal cards |
| `analyst_report_id` uuid NULL, indexed | M13's one-hop join for `/pulse` cards |
| `market`, `symbol` | so M13 can filter without joining back to `signals` |
| `summary_text` text | the Persian words, **without** the reference line — that is appended at render time, so its wording can change without a migration and no stored row can carry a superseded sentence |
| `input_text` text | exactly what the model was shown. The evidence that it saw a card and nothing else, and what makes the numbers rule auditable after the fact rather than only at the moment of generation |
| `prompt_version`, `model` | a `v2` must be distinguishable from `v1` in stored rows, the same reason every other table here carries them |
| `tokens_in`, `tokens_out`, `cost_usd_estimate` numeric(18,8) | what this row cost. Also the input to the deployment-wide daily cap |
| `llm_call_id` uuid NULL | points at the `llm_calls` audit row for the call that produced it |
| `created_at`, `created_by_user_id` | the per-user daily cap's query, and who paid |

Indexes, all on the new table only: `uq_persian_summaries_input_sha256`,
`ix_persian_summaries_signal_id`, `ix_persian_summaries_analyst_report_id`,
`ix_persian_summaries_user_created_at (created_by_user_id, created_at)` — composite in
that order because the user is the equality predicate and the timestamp is the range one.

### No foreign keys, deliberately

An FK to `signals` would install a constraint trigger on the **referenced** table, which
means deleting a signal would depend on this table and `signals` would gain a dependency
edge. §F requires that deleting every row here breaks nothing else, and Constraint 1
requires `signals` to stay exactly as it is. A plain indexed UUID gives M13 its join and
keeps the dependency strictly one-directional.
`test_deleting_every_row_leaves_the_signals_table_alone` asserts it rather than arguing
it, and `assert_signals_untouched_by_0012` in `ops/verify-migration.sh` asserts on
deploy day that `signals` has acquired no foreign key.

### The round trip, against the real Postgres suite

`sentinel_test` on the local Compose Postgres, carrying the full schema at
`0011_forex_spine`:

```
$ alembic current
0011_forex_spine

$ alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade 0011_forex_spine -> 0012_persian_summaries, Persian card summaries

$ psql -c "\d persian_summaries"
                        Table "public.persian_summaries"
       Column       |           Type           | Collation | Nullable | Default
--------------------+--------------------------+-----------+----------+---------
 id                 | uuid                     |           | not null |
 input_sha256       | character varying(64)    |           | not null |
 source_kind        | character varying(16)    |           | not null |
 signal_id          | uuid                     |           |          |
 analyst_report_id  | uuid                     |           |          |
 market             | character varying(16)    |           | not null |
 symbol             | character varying(32)    |           | not null |
 summary_text       | text                     |           | not null |
 input_text         | text                     |           | not null |
 prompt_version     | character varying(32)    |           | not null |
 model              | character varying(64)    |           | not null |
 tokens_in          | integer                  |           | not null |
 tokens_out         | integer                  |           | not null |
 cost_usd_estimate  | numeric(18,8)            |           | not null |
 llm_call_id        | uuid                     |           |          |
 created_at         | timestamp with time zone |           | not null |
 created_by_user_id | bigint                   |           | not null |
Indexes:
    "pk_persian_summaries" PRIMARY KEY, btree (id)
    "ix_persian_summaries_analyst_report_id" btree (analyst_report_id)
    "ix_persian_summaries_signal_id" btree (signal_id)
    "ix_persian_summaries_user_created_at" btree (created_by_user_id, created_at)
    "uq_persian_summaries_input_sha256" UNIQUE CONSTRAINT, btree (input_sha256)

$ alembic downgrade 0011_forex_spine
INFO  [alembic.runtime.migration] Running downgrade 0012_persian_summaries -> 0011_forex_spine, Persian card summaries

$ psql -c "select to_regclass('public.persian_summaries')"
 still_there
-------------

(1 row)

$ alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade 0011_forex_spine -> 0012_persian_summaries, Persian card summaries

$ alembic current
0012_persian_summaries (head)
```

And the assertion the whole constraint is about — `\d signals` captured before the round
trip and after it, diffed:

```
######## signals table shape: before the round trip vs after ########
no diff — the signals table did not move
```

`ops/verify-migration.sh` now targets `0012_persian_summaries` (`PREVIOUS_REVISION`
stays at `0009`, so the round trip spans three upgrades and three downgrades) and gains
`assert_persian_table_absent` and `assert_signals_untouched_by_0012`. `make check-ops`
`bash -n`s it clean; shellcheck is not installed on this machine, which the target
reports rather than skipping silently.

---

## 5. The three real calls

`python -m sentinel.tools.persian_summary_cost --call`, `claude-sonnet-4-6`. Run 1 was
made against the pre-H1 prompt and is **superseded** — it is kept because the count of
calls, and one of their outcomes, is evidence about the rails.

| | run 1 (superseded) | run 2 — WATCHLIST | run 3 — CANDIDATE |
|---|---|---|---|
| card | `card_shared.txt` | rendered `/pulse` WATCHLIST | `card_shared.txt` |
| cache | cold | **warm** | cold |
| input / write / read | 1,426 / 0 / 0 | 389 / 0 / 1,031 | 594 / **1,031** / 0 |
| output | 480 | 373 | 436 |
| latency | 13.3 s | 9.7 s | 12.6 s |
| length | 655 chars | 509 chars | 611 chars |
| verdict rail | (did not exist) | **PASS** | **PASS** |
| numbers rail | PASS | **PASS** | **PASS** |
| cost | $0.011478 | **$0.007071** | **$0.012188** |

**The rails are set on $0.012188**, the cold figure — the pessimistic one, and the one an
isolated press pays.

### The amendment switched caching on, and no token count would have predicted it

The system prompt was ~700 tokens. **Anthropic does not cache a block below 1,024**, so
`cache_control` was a no-op and run 1 paid full input at $3/Mtok. H1's verdict rule
pushed the block to **1,031 tokens**, and run 3 paid a 1,031-token cache *write* at
$3.75/Mtok — a per-press cost **6% higher** than run 1 despite a shorter card — while run
2 read the same block back at $0.3/Mtok and cost **42% less**.

So a longer prompt made isolated presses dearer and bursts much cheaper. This is
journal/M10d_REPORT.md §8's lesson arriving from the opposite direction: there an
estimate missed a mechanism that made things cheaper; here a mechanism made them dearer.
**A claim I made in the first version of this report — "the system block is always cold"
— was an overclaim**, and it was wrong twice: cold presses were not paying for a cache at
all, and presses inside five minutes of each other do share one.

**The tool was hiding the component that explains all of it.** It printed `input` and
`cache read` and not `cache write`, so the change looked like a mystery. It prints every
component now.

### A fourth call, not in the table, and the one worth remembering

The first attempt at run 2 was **rejected by the numbers rail** — the model wrote a
figure that was not on the card — and it could not be diagnosed, because
`SummaryResult` discarded the text of a rejected summary and the tool then re-checked
the empty string and printed **`PASS` about nothing**.

That is §11's "watch the rejection rate" being unactionable on its first real rejection.
Both the result type and the tool now carry the rejected text, and the tool reports the
rails' own verdicts instead of re-deriving them. `text` is what the model returned on
every outcome; whether it may be *sent* is `ok`, and two tests assert the sending half.

So of **three** post-amendment calls, one tripped a rail. Too small a sample to be a
rate; large enough to say the rails are not decorative.

### The full output of run 3, verbatim

```
✅ شرایط خوبه — بیتکوین لانگ داره

✅ چی خوبه
- روند کلی صعودیه و قیمت به یه ناحیه تقاضا پولبک زده
- سه تارگت داره: 65100.0، 65900.0 و 67000.0
- ریسک به ریوارد خوبیه — تارگت اول تنها 1.75R سود داره

⛔ چرا الان نه
- فاندینگ کمی شلوغه و لانگ‌های زیادی توی بازارن
- اگه قیمت پر نشه تا 2026-08-19 03:00 EEST، سیگنال منقضی میشه

🔑 شرط ورود
اگه قیمت به محدوده 64150.0 تا 63800.0 برسه، اونوقت با لیمیت اردر وارد میشی.

❌ چی خرابش می‌کنه
بسته شدن کندل یک‌ساعته زیر 63450.0 کل ایده رو باطل می‌کنه — استاپ همونجاست.

مثل اینه که منتظری قطار یه‌کم عقب بره تا با قیمت بهتر سوارش بشی — ولی اگه از ریل خارج شد، دیگه سوار نمیشی.
```

Three things worth noticing:

- **"سه تارگت" — it counted in words.** HARD BOUNDARY 4 forbids writing any digit not on
  the card, *including counts*, precisely so counting is never a reason for the numbers
  rail to reject a true summary. It obeyed, unprompted, in both runs.
- **All twelve numbers are on the card**, including the expiry timestamp, which it read
  correctly as a reason not to wait for ever.
- **The closing metaphor is the product.** "Like waiting for the train to roll back a
  little so you can get on cheaper — but if it comes off the rails, you don't." That is a
  limit-order entry with an invalidation, said in a way the dense English card cannot.

### And run 2, the WATCHLIST — the case the verdict rail exists for

```
❌ الان نخر — فقط تماشا کن

👀 چی خوبه
- روند 4 ساعته هنوز صعودیه
- قیمت داره به EMA50 و سطح بریک‌اوت 82.4 پولبک می‌زنه
- Open Interest داره با قیمت بالا میره

⛔ چرا الان نه
- شاخص ترس و طمع روی 74 (طمع) — یعنی بازار شلوغه و ریسک ازدحام لانگ بالاست
- رزیستنس مهم روی 84.6 نزدیکه

🔑 شرط ورود
اگر قیمت بالای 82.4 تثبیت شد و شرایط بهتر شد، اونوقت می‌شه دنبال پلن گشت.

❌ چی خرابش می‌کنه
کندل 1 ساعته زیر 81.40 بسته بشه — ایده تموم‌شده.

فعلاً این سهم مثل ماشینیه که موتورش گرم‌شده ولی هنوز چراغ سبز نداره — صبر کن.
```

It opened with the owner's own example wording, and the entry condition is correctly
conditional — *if it holds above 82.4 and conditions improve, then it is worth looking
for a plan* — rather than an instruction to buy. This is the text the teeth test in §7
alters.

## 6. The numbers rail (owner requirement B)


`sentinel/analyst/persian/numbers.py`. Pure — no clock, no database, no LLM, no I/O.

Every numeric token in the output must appear in the input, after **one normalisation
applied identically to both sides**:

1. strip markup and restore the three escaped entities, so `<b>64150.0</b>` and
   `64150.0` tokenise the same;
2. extract with one regex, `[0-9]+(?:[.,][0-9]+)*`, applied to both sides — which is what
   makes `EMA200` on the card and `EMA 200` in the summary yield the same token;
3. remove thousands separators, and nothing else;
4. assert `tokens(output) ⊆ tokens(input)`. One direction: the summary is a short rewrite
   and is expected to drop numbers.

### Persian digits: ASCII only, and the check agrees with the prompt

The rule is that numbers are copied character-for-character, and a Persian-digit form is
by definition not that. Allowing `U+06F0..U+06F9` would mean normalising two numeral
systems on the one rail that must never be wrong, in exchange for a rendering
preference — and every price on every card is ASCII already. So a Persian or
Arabic-Indic digit anywhere in the output is an **outright failure**, not something to
convert and compare. `test_persian_digits_fail_even_when_the_value_is_right` asserts
that the correct stop, written in Persian digits, still fails.

### The line between "normalised" and "reformatted"

A thousands separator is a **grouping mark**: removing it cannot change what the number
is, so `64,150.0` and `64150.0` are the same number written for two audiences. A dropped
trailing zero is not — `4570.3` and `4570.30` differ in stated precision, which on a stop
price is a claim about how exact the level is. So the first is normalised away and the
second fails. That is what "never reformatted" has to mean if it is to be checkable at
all, and `test_a_rounded_number_fails` and
`test_a_decimal_comma_is_not_a_grouping_mark_and_fails` pin both sides of the line.

### Why containment and not "compare only prices"

Working out *which* token is a price requires the checker to read the card — which is
the one thing this design refuses to let anything downstream of the analyst do.
Containment is strictly stronger and needs no understanding: it does not ask whether a
number is right, it asks whether it was **copied**.

### What it fails closed on, and what it does not

On any violation the summary is **not sent and not stored**; the reader gets a short
Persian sentence; `persian.number_check_failed` is logged with the offending tokens; and
the `llm_calls` row **is** written, because the money was spent either way. Because
nothing was cached, a retry is possible — `test_a_rejection_can_be_retried_because_
nothing_was_cached`.

**The residual risk, named rather than left implicit:** the check cannot catch a number
copied correctly but attached to the wrong label — a stop presented as a target. A
label-adjacency check is possible and brittle; it is an open item for the review, not a
silent gap.

Nor can it catch a **softened verdict**, which invents no number at all — that is what
§7's second rail is for, added at the owner's instruction after the first version of this
report recorded the gap.

### The proof of teeth

`test_the_check_would_catch_a_changed_value` perturbs the card's stop `63450.0` by **one
digit at the last place** — same magnitude, same scale, same character count — following
HANDOFF §4 item 13's rule that a proof which mutated the number beyond recognition would
pass against a check that had stopped working.

---

## 7. The verdict rail (owner requirement H1)

**My assessment first, since you asked for it rather than for compliance: build it, and
the case is stronger than your note put it.**

| | signal card | `/pulse SYMBOL` |
|---|---|---|
| reachable verdicts | **CANDIDATE only** | CANDIDATE / WATCHLIST / NO_SETUP |
| why | `_check_preconditions` returns `NOT_A_CANDIDATE` before a plan is built, so there is no card | the drill-down renders whatever the analyst last said |
| distinct numeric tokens | 40 | **15** — 6 date parts, 2 confidence, 4 timeframe labels, **3 levels**, all from prose |

So the surface where a softened verdict is reachable is also the surface where **the
numbers rail is nearly inert**. It is not that the verdict rail adds a second check
beside a working one; on `/pulse` it is very nearly the only check there is. That
settled it.

### The design, and the two things that make it a rail rather than a nuisance

**1. The verdict is supplied, never parsed.** `check_verdict(summary=…,
candidate_status=…)` takes the status from the report the card was rendered from —
`plan.report.candidate_status` for a signal, `row.candidate_status` for a pulse card. It
is never read out of the card text and never out of the model's answer. A check that read
the model's own words to decide what the model was supposed to say would be agreeing with
itself. `test_the_verdict_rail_is_checked_against_the_stored_report` proves it by moving
the stored verdict while the card and the answer stay identical.

**2. One leading character, not a phrase vocabulary.** This is where the "unworkable"
version of your idea lives. Persian has many good ways to say *do not buy* — نخر, صبر کن,
فقط تماشا, وارد نشو, دست نگه دار — and a fixed phrase list rejects the ones nobody thought
of. **A rail that fires wrongly on a true summary is worse than the gap it closes**, and
you said so yourself. A single leading emoji has none of that: the prompt can mandate it
exactly, it costs the writing nothing, your own examples already open that way, and the
very first live call produced one *before it was asked to*.

```
✅              actionable
❌  ⛔  👀       not yet
```

Two classes, not three. `WATCHLIST` and `NO_SETUP` share `NOT_YET` because the
distinction that matters is actionable-versus-not; separating them would catch a
NO_SETUP reading like a WATCHLIST — far less consequential — at the cost of a third
marker set and a third way to throw away a good summary. The sets are asserted disjoint,
and anything that is not `CANDIDATE` falls to `NOT_YET`, including a status this rail has
never heard of: a verdict it cannot classify must not be allowed to read as a buy.

The prompt and the rail are pinned to **the same table**:
`test_the_persian_prompt_mandates_the_marker_the_verdict_rail_checks` iterates
`VERDICT_MARKERS` and asserts each marker appears in the prompt, so adding one without
teaching the prompt about it fails there rather than in production.

### Proof of teeth, against real model output

Run 2's WATCHLIST summary (§5) is kept verbatim in the test file as
`REAL_WATCHLIST_SUMMARY`. The test alters **one line** of it — the opening verdict — and
nothing else:

```
before   ❌ الان نخر — فقط تماشا کن          "don't buy — just watch"
after    ✅ شرایط خوبه — می‌تونی وارد شی      "conditions are good — you can go in"

numbers identical?        True   (8 tokens, unchanged)

  numbers rail    PASS — every number in the summary is on the card
  verdict rail    PASS — original: the verdict line matches NOT_YET
  verdict rail    FAIL — softened: verdict line '✅ شرایط خوبه — می‌تونی وارد شی'
                         does not start with ⛔ ❌ 👀 — the card is NOT_YET
```

The test asserts the numbers claim in its sharpest form — `numeric_tokens(softened) ==
numeric_tokens(original)` — so it does not depend on which card it is checked against:
**the alteration changed no number at all**, therefore no rail built on numbers can ever
see it. That is the blindness, demonstrated rather than asserted.

`test_the_real_watchlist_summary_passes` sits beside it as the baseline, because without
it the teeth test would also pass against a rail that rejected everything.

### What it does not prove, recorded as a test

`❌ بخر` — do-not-buy marker, buy text — **passes**. It checks the marker, not the
meaning. `test_the_rail_checks_the_marker_and_not_the_meaning` says so in the suite so
the limit is where somebody will read it, rather than in a comment. Catching that needs
the phrase vocabulary this deliberately does not have, and the trade is unchanged: false
failures on true summaries cost more than this residual.

### Where it sits, and why in `numbers.py`

In the same module as the numbers check, as you asked. The module name now
under-describes what is in it, which is the smaller cost — the two are the only
fail-closed checks on this path and they cover each other's blind spot, and splitting
them across two files is how one gets reviewed, loosened or deleted without the other
being in front of the reader. The docstring says exactly that.

### One more thing it changed

The rail runs **before** the numbers rail, so a summary can be `VERDICT_REJECTED` without
the numbers ever being checked — reported as "not reached" rather than as a pass. A tool
that printed `PASS` for a check that never ran is the defect §5 describes, one level
along.

## 8. The reference line (owner requirement G2)

> **«این فقط یک خلاصه ساده است. مرجع اصلی همان کارت انگلیسی بالاست.»**
> *This is only a simple summary. The original reference is the English card above.*

On **every** message this feature can send, including all four failure messages — those
are the ones most likely to be read as the system having an opinion.

It is a module constant in `bot/persian_cards.py`, appended **by code after the model
returns**, in the position `formatting.DISCLAIMER` occupies on the English card. It is
**not** in the prompt, and `test_the_persian_prompt_never_asks_the_model_for_the_
reference_line` asserts its absence there, so "just add it to the prompt" fails the
suite. A rail the model can decline to emit is not a rail.

The prompt's header states *why*, for whoever edits it later: the Persian path is a
convenience surface with no risk engine behind it — a cheaper model rewriting a more
expensive model's words. If the two ever disagree, the English card is authoritative; it
is the one that moves real money and the one `sentinel/risk/` produced. A friendlier card
in the reader's own language is trusted **more**, not less, which is precisely why it has
to point back at the authority.

---

## 9. Spend (owner requirement E)

### Crypto's reserved floor does not protect this path, and it is worth being exact

`markets.crypto.llm_reserved_floor_usd: 8` holds the **unspent** part of crypto's budget
against **other markets**. The market exposed here is **forex**, which has no floor at
all: its real ceiling is `llm_daily_budget_global_usd − max(0, 8 − crypto_spent)` = **$12**
against a measured need of **$8.81/day**. That leaves about **$3.19 of slack** and
nothing defending it. A Persian path drawing from the global ceiling eats that first.

So the floor answers a different question, and something new was needed.

### Two rails, neither touching `sentinel/llm/spend.py`

1. **`daily_usd_cap: "0.60"`** — every Persian call, every user, per UTC day.
2. **`daily_generations_per_user: 20`** — **generations, not presses.** A press that hits
   the cache costs nothing, and rationing a free action would be a rail that only annoys.

Both are checked **before** the call (`test_..._the_cap_is_checked_BEFORE_the_call`).

**The arithmetic, from the measured $0.012188 (cold):** two approved users × 20 =
**$0.4875**, under a $0.60 ceiling with 23% to spare — enough that the per-user cap
reliably exhausts first however long a card runs, rather than the two rails racing. So
the per-user cap is the one that bites and the USD ceiling is the backstop for a third
user. $0.60 is **4.8%** of an ordinary $12.42 day and **19%** of forex's $3.19 of slack.
Worst case it adds 3% to the $20 global ceiling. Real totals sit below this, because a
press inside the five-minute cache window costs $0.007071 rather than $0.012188.

**Written as `"0.60"`, quoted** — YAML parses `0.50` as the float `0.5`, which becomes
`Decimal("0.5")` and logs as `of $0.5`. The same trap the crypto budget comments at the
top of `config.yaml` describe. It is never rendered on a card, but a rail whose log line
reads oddly is one nobody double-checks.

### Where the cost is visible, at no cost to any surface

Every Persian call is recorded in `llm_calls` with `kind = PERSIAN_SUMMARY` and the
card's market — so it appears on `/status` and the owner-only `/pulse` spend line with
**no surface change and no golden movement**. The kind is a member of `LLMCallKind` so a
later reader can separate convenience spend from analysis spend by looking, rather than
inferring it from the model name. Fifteen characters against `llm_calls.kind`'s
`String(16)`, and the column is a plain varchar — no `ALTER TYPE`.

### One deliberate non-consultation

The Persian path does **not** call `evaluate_market_spend`. A paused *analysis* budget
should not break a rewrite of a card that already exists, and the two caps above already
bound the exposure. The rails the two live measurement windows depend on are not read,
not written and not modified by this feature.

The daily USD cap is read from **this table**, not from `llm_calls`, even though both
hold the figure — a cap that queried `llm_calls` with a `kind` filter would put a
Persian-shaped predicate inside the query path the analysis rails use.

---

## 10. The button, the handler, and the double tap

- 🇮🇷 فارسی on crypto signal cards, forex signal cards and the **last page** of
  `/pulse SYMBOL` (the card splits for Telegram's 4,096-character limit but is one
  verdict to a reader; the summary covers every page, rejoined).
- Press → the query is answered immediately → the Persian text arrives as a **reply** to
  that message. A reply rather than an edit: the English card is the reference the
  Persian text points back at, and rewriting it in place would remove what is pointed at.
- The button does not disappear. `_keyboard_for` **replaces** the whole markup after a
  decision, so the row is re-composed there too — without that, the button would vanish
  the moment the owner pressed Taken, which is exactly when he is most likely to want the
  card explained. `test_the_persian_button_survives_a_decision`.
- Any user the auth gate passes may press. For signal cards the handler re-checks
  `row.user_id != actor.user_id` against the database, because a forwarded card carries
  its buttons.
- Every failure path — no API key, cap reached, API error, failed numbers check, missing
  card, unexpected exception — sends a short Persian sentence. Never silence, never an
  English traceback. `answers_on_failure` is message-only, so the callback handler
  carries its own wrapper, and
  `test_the_persian_button_is_reachable_by_owner_and_member` asserts on **real
  dispatcher machinery** that the router is actually wired — HANDOFF §4 lesson 1, since a
  dead router and a correctly refused press look identical from outside.

### The in-flight coalescer, and what it is not

This bot has no other in-memory guard, on purpose — every guard here is database-backed.
The durable half of this one is the unique index: a second press reads the stored row and
makes no call.

What remains is a genuine race: two presses in the same second both miss the cache, both
buy a call, and one loses the `ON CONFLICT` — **after** its money is spent. So the
in-memory piece is an **in-flight coalescer** (`dict[str, asyncio.Future]` keyed by the
card hash), not a rate limiter: the second press awaits the first's result. It is
process-local, which is correct for a single-process bot and honest about what it covers.
The brief's "short in-memory guard against double-taps within a few seconds" is a second,
separate thing and is also there: `double_tap_seconds: 5`, per user.

`test_two_simultaneous_presses_buy_one_call` drives both concurrently through
`asyncio.gather`.

---

## 11. Numbers

| | |
|---|---|
| `make check` | **exit 0** |
| suite, without a scratch DB | 2,212 passed, 74 skipped |
| suite, with `SENTINEL_TEST_DATABASE_URL` | **2,285 passed, 1 skipped** |
| new tests | **85** across four new files, plus 9 in `test_prompts.py` and 3 in the wiring/callback suites |
| rails, both fail-closed | 2 |
| golden text files moved | **0** |
| golden files added | 1 (`card_shared.txt`) |
| migrations | 1, add-table only, downgrade exercised |
| frozen diff | empty |
| measured cost per press | **$0.012188** cold · $0.007071 warm |
| real calls made | 4 — three in §5's table, one rejected by a rail |
| deployed | **no** |

---

## 12. What is still NOT verified

- **The rejection rate of either rail.** Four calls, one rejection, is not a rate.
  Whether the model obeys reliably can only be answered in service. The two log events
  are `persian.number_check_failed` and `persian.verdict_check_failed`, and the pressure
  to expect — and refuse — is to loosen a rail rather than to write a `v2` prompt.
- **A softened verdict that keeps the right marker.** `❌ بخر` passes §7's rail. Nothing
  checks meaning, and nothing cheaply can.
- **A number attached to the wrong label.** See §6. Named, not built.
- **The forex card's Persian output.** `forex_signal_card(shared_only=True)` is tested
  structurally and has never been sent to the model. Its card is longer than crypto's,
  so the 900-character ceiling is more likely to bind there first.
- **`/pulse SYMBOL` end to end.** The re-render path is unit-tested; no press has ever
  been made against a real stored report.
- **Anything on the server.** Nothing is deployed. The table does not exist in
  production and the button is not on any live card.

### The joins this milestone's own boundary leaves untested

HANDOFF §4 lesson 11 — a milestone boundary is untested by construction, so the next
session composes first and builds second. Three here:

1. **Migration ↔ a real production dump.** The round trip in §4 ran against
   `sentinel_test`, which carries the schema and almost no rows.
   `ops/verify-migration.sh` is the tool that closes this and runs against a restored
   backup; it has been updated for `0012` but **not run** — it needs a dump.
2. **`build_summariser` ↔ the live process.** The bot has never held an LLM client
   before. It is constructed once at `build_context` and never closed, which is right for
   a process-lifetime component, but no long-running process has exercised it.
3. **The button ↔ a real Telegram card.** Every test drives a fake query. The first real
   press is the first time `reply_to_message_id` threading, RTL rendering of Persian
   beside ASCII prices, and the emoji bullets are seen by a person.

---

## 13. How to demo

```bash
python -m sentinel.tools.persian_summary_cost            # free: card size, token count
python -m sentinel.tools.persian_summary_cost --call     # ONE real call, ~$0.011
```

The second prints the measurement table from §5, runs the numbers check over the result,
says how many presses fit inside the daily cap, and prints the Persian in full.

On the server, after a deploy: press 🇮🇷 فارسی on any signal card. A second press returns
the identical stored text and costs nothing.

**Rollback** is `persian_summary.enabled: false` and a restart — one line, no deploy of
code. Dropping the table loses cached text and nothing else: every row is reproducible
from the card it summarises.

---

## 14. Open questions for the review

1. **Should `/pulse` (the cycle summary) get the button too?** Read as no (§1d). If the
   owner wants it, the input would need a rule for the owner-only spend line.
2. **Should the verdict rail check meaning as well as the marker?** ~~Should a softened
   verdict have a rail at all~~ — answered and built (§7). What is left is `❌ بخر`, which
   passes. The next step is a phrase vocabulary, and its cost is false failures on true
   summaries. **Decide it from `persian.verdict_check_failed` counts, not from taste.**
3. **Is 20 generations per user per day right?** It is a bound, not a measurement: 15
   would still cover an owner walking the whole watchlist once, and 20 was chosen so the
   per-user rail exhausts at $0.4875 under a $0.60 ceiling. Re-derive from
   `persian.capped` log lines after a week.
4. **`forex_cards.py` and the no-arithmetic guard** (§1e) — M9 hygiene.
5. **Does the system prompt need a floor?** It is 1,031 tokens against Anthropic's
   1,024-token caching minimum — **seven tokens of margin**. Trimming it would silently
   switch caching off and make bursts of presses ~70% dearer, a cost change with no
   cost-shaped cause. Either leave a comment on the prompt file, or decide the caching
   is not worth depending on.
