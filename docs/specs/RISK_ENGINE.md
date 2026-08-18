# SPEC — Deterministic Risk Engine

**Module:** `sentinel/risk/`
**Rule zero:** No LLM anywhere in this module. Pure, unit-tested functions. 100% branch coverage required. All money math uses `Decimal`.

---

## 1. Inputs

From runtime config (DB-overridable via Telegram):

| Param | Default | Meaning |
|---|---|---|
| `capital_eur` | set by `/capital` (required before first signal) | Owner's total trading capital in EUR |
| `risk_per_trade_pct` | 0.75% (allowed 0.25–1.5%) | Max loss if full ladder fills and stop hits |
| `max_open_risk_pct` | 2.25% (= 3 trades worth) | Sum of open risk across taken signals |
| `max_leverage` | 10x | Hard cap on suggested leverage |
| `margin_budget_pct` | 10% | Target margin per trade as share of capital |
| `daily_loss_limit_pct` | 3.0% | Realized loss (R-equivalent) → auto-pause 24h |
| `max_positions` | 4 | Concurrent taken signals |
| `min_rr_tp1` | 1.5 | Reject plans below this — **measured NET of costs** (§4.2) |
| `eurusd_rate` | fetched hourly, cached | For USDT→EUR display conversion |
| `maker_fee_pct` | 0.02% | Entry legs (the limit ladder) — §4.2 |
| `taker_fee_pct` | 0.05% | Every exit leg: stop, TP, trail, manual — §4.2 |
| `funding_interval_hours` | 8 | Settlements per day assumed when estimating funding — §4.2 |
| `credit_favourable_funding` | false | Whether a funding *credit* may raise net RR — §4.2 |

> **Correction (2026-08-18, from M5.1) — `min_rr_tp1` gates NET reward-to-risk.**
> Through M4 and M5 it gated the gross figure, because this spec costed nothing.
> The parameter's value is unchanged at 1.5, but **the quantity it measures is
> not**: it is now reward-to-risk after fees and estimated funding (§4.2). This is
> a real tightening — the four §8.1 golden cases all sat at 1.50–1.51 gross and
> land at 1.25–1.44 net, so setups the system approved through M5 are now
> rejected, with the distinct code `NET_RR_TOO_LOW`. Tighter-stopped setups lose
> the most, because cost as a share of the risk budget is ≈ `0.07% / stop_distance`.
> **When tuning this parameter during M9, tune it as a net threshold.** A gross
> equivalent of the old behaviour does not exist as a single number: the gross-to-net
> gap depends on the stop distance, from ~0.07R on a 2.3×ATR stop to ~0.25R on a
> 0.6×ATR one.

From the analyst (`AnalystReport`): direction, entry_zone {low, high}, stop, targets[], setup_type, confidence.

## 2. Coherence checks (reject with reason if any fail)

1. Long: `stop < entry_zone.low < entry_zone.high` and all targets > entry_zone.high. Short: mirrored.
2. Entry zone within `max_entry_distance_pct` (default 3%) of last price — no chasing, no fantasy fills. **(Ruling 2026-08-18, from M4: measured on BOTH edges, so every rung is a plausible fill.)**
3. Stop distance ≥ 0.6 × ATR(14, 1h) — rejects noise-level stops that guarantee stop-outs.
4. Stop distance ≤ 3 × ATR(14, 1h) — rejects lazy wide stops that wreck RR.
5. RR to TP1 (from weighted avg entry) ≥ `min_rr_tp1`. **(Correction 2026-08-18,
   from M5.1: measured NET of fees and estimated funding — see §4.2. The gross
   check remains as a cheap early reject, since net ≤ gross always; the gate that
   ships a plan is the net one, and it rejects with `NET_RR_TOO_LOW`.)**
6. Confidence ≥ 60 for CANDIDATE processing (below → downgrade to WATCHLIST).
7. Portfolio: open risk + this trade ≤ `max_open_risk_pct`; positions < `max_positions`; symbol not on cooldown; not paused.

## 3. Entry ladder construction (multi-entry)

The analyst supplies a zone; the engine decides the ladder deterministically:

- **Zone width < 0.5 × ATR(1h)** → single entry at zone midpoint (weight 100%). Momentum/breakout setups typically land here.
- **Zone width ≥ 0.5 × ATR(1h)** → 3-rung ladder for pullback/range setups:
  - Rung 1: zone edge nearest current price, weight **40%**
  - Rung 2: zone midpoint, weight **35%**
  - Rung 3: zone far edge (best price), weight **25%**
- Weighted average entry `E = Σ(price_i × w_i)` is used for all sizing and RR math.
- Ladder metadata stored so the tracker can account partial fills: if only rung 1 fills and stop hits, realized loss = 40% of planned risk (≈ −0.4R), and the card explains this.

> **Correction (2026-08-18, from M4) — the weights are shares of RISK, not of notional.** This
> section and §8.5 both promise that a rung-1-only stop-out is −0.40R. That is only true if each
> rung takes `w_i` of the *risk budget*: `qty_i = risk_usdt × w_i / |p_i − stop|`. §4's
> `qty_i = (notional × w_i)/p_i` makes them shares of notional instead, under which rung 1 —
> nearest price and therefore furthest from the stop — carries **51.3%** of the risk, and a
> rung-1-only stop-out is −0.51R. Owner ruling: **§3/§8.5 win**; §4's qty formula is superseded.
> The two readings coincide exactly for a single-entry plan. See `journal/M4_REPORT.md`.

> **Ruling (2026-08-18, from M4) — collapse order.** §4 says "collapse to fewer rungs (3→2→1)
> preserving total notional" without saying which rung goes. The engine drops the **far rung** (the
> smallest weight) and renormalizes the survivors proportionally (40/35 → 53.33/46.67); a single
> surviving rung sits at the zone midpoint at 100%, matching the narrow-zone rule above. Because a
> collapse moves `E`, rules 3–5 are re-checked against the final ladder before the plan ships.

## 4. Sizing & leverage math

```
risk_eur        = capital_eur × risk_per_trade_pct
stop_dist_pct   = |E − stop| / E
notional_eur    = risk_eur / stop_dist_pct          # full-ladder notional
notional_usdt   = notional_eur / eurusd_rate → converted for qty math
qty_i           = (notional × w_i) / price_i        # rounded DOWN to exchange qty step
margin_eur      = capital_eur × margin_budget_pct
leverage_raw    = notional_eur / margin_eur
leverage        = clamp(ceil_to_step(leverage_raw, 1), 1, max_leverage)
margin_eur_final= notional_eur / leverage           # recomputed after clamping
```

> **Correction (2026-08-18, from M4) — EUR→USDT multiplies, it does not divide.** The line
> `notional_usdt = notional_eur / eurusd_rate` above is wrong for the rate we actually store. M1
> fetches Frankfurter with `base=EUR&symbols=USD`, so `FxRate.rate` is **USD per EUR** (1.1593), and
> the conversion is `notional_usdt = notional_eur × eurusd_rate`. Dividing undersizes every position
> by ~26% (1/1.1593² ≈ 0.744). The engine multiplies.

> **Correction (2026-08-18, from M4) — `qty_i` is derived from risk, not from notional.** See the
> ruling in §3: `qty_i = risk_usdt × w_i / |p_i − stop|`, and the notional is the *result*
> (`Σ qty_i × p_i`) rather than the input. `notional = risk / stop_dist_pct` remains exactly true
> for a single-entry plan, and is within a fraction of a percent of the ladder result otherwise.

**Liquidation buffer rule:** approximate isolated-margin liquidation distance ≈ `1/leverage` (conservative, ignoring maintenance margin tiers in v1). Require:

```
liq_distance_pct ≥ 2.0 × stop_dist_pct
```

If violated, reduce leverage until satisfied; if leverage would fall below 1, reject. This guarantees the stop always triggers far before liquidation — the position can never be liquidated on a planned stop-out.

**Min notional rule:** each rung's notional ≥ `max(exchange_minimum, 20 USDT)`. If a rung falls below, collapse to fewer rungs (3→2→1) preserving total notional.

> **Correction (2026-08-18, from M1):** the earlier "Binance ≈ 5 USDT" was wrong — the exchange minimum is **per symbol**, not a global constant, and it can exceed the 20 USDT practicality floor. Measured live via `ccxt` `limits.cost.min` on Binance USDT-M: **BTCUSDT = 50 USDT**, **SOLUSDT = 5 USDT**. So the rule is a `max()`, never a fixed 20: the engine must read `InstrumentMeta.min_notional` (ingested and cached per symbol since M1) and take the larger of it and the 20 USDT floor. Hardcoding 20 would produce rungs Binance rejects on BTCUSDT.

**Rounding:** qty down to symbol qty-step; prices to symbol tick-size (fetched from exchange info, cached daily). After rounding, recompute actual risk_eur and display the real number.

> **Correction (2026-08-18, from M6) — human-facing percentages are 2dp.**
> `stop_distance_pct`, `liq_distance_pct`, `cost_pct_of_risk` and the M6 distance
> fields were quantized to 4dp. Owner ruling: **2dp, trailing zeros trimmed** — a
> percentage on a card is read, not typed into an order field, so `+3.0541%` offers
> four digits nobody can act on and `20.0000%` implies the liquidation estimate was
> measured rather than derived from `1/leverage`. It now renders `+3.05%`, `20%`,
> `4.45%`.
>
> **Prices, euro amounts and quantities are unchanged.** Those *do* go into order
> fields, and their precision is the exchange's tick and step grid (above), not a
> display choice.
>
> This changes what is shown and never what is approved: every caller of `percent()`
> is a display field, and the liquidation-buffer rule, the ATR bounds and both RR
> checks all run on unrounded fractions. One consequence is asserted rather than
> assumed — §8.2's `liq_distance_pct >= 2 x stop_distance_pct` invariant now compares
> two 2dp figures, so it carries a one-quantum tolerance; the underlying inequality
> is still exact.

## 4.2 Transaction costs

> **Correction (2026-08-18, from M5.1) — this spec costed nothing, and that biased
> every RR figure it produced.** Fees and funding were absent from §4 entirely, so
> `rr_tp1` was gross and `min_rr_tp1` gated a number the owner never actually
> collects. On M5's live ETHUSDT plan the gap was ~16% of the risk budget: a 0.45%
> stop implies €17.3k of notional for a €75 risk, on which a maker-in / taker-out
> round trip is ~€12. Left uncorrected the bias would have propagated into M9's
> measured performance, not just the cards.

**Rates.** Binance USDⓈ-M VIP 0, verified against the published schedule on
2026-08-18 (<https://www.binance.com/en/fee/futureFee>): **maker 0.0200%**, **taker
0.0500%**. The BNB discount (−10%) and the VIP volume tiers are deliberately not
modelled — these are the worst-case rates actually paid, and a cost estimate that
flatters the trade is worse than none. They live in `config.costs`, not in code.

**Which leg is which.** Entries are the maker leg: the ladder is limit orders.
**Every exit is priced as taker** — stop, TP, trail and manual alike (owner ruling
2026-08-18). A TP may well rest as a limit and earn the maker rate, but assuming so
would let net RR depend on a fill quality the plan cannot promise. Same conservative
bias as flooring quantities and rounding stops away from the entry.

```
Q             = Σ qty_i
entry_fee     = Σ (qty_i × p_i / rate) × maker_fee     # per rung, at its own price
stop_exit_fee = (Q × stop / rate)      × taker_fee     # part of net RISK
tp_exit_fee_i = (Q × TP_i / rate)      × taker_fee     # part of net REWARD
```

**Funding.** Estimated, never asserted — it is the one input here that changes
under the plan. Settlements are counted in `(created_at, expires_at]` by stepping
`funding_interval_hours` from the snapshot's published `next_funding_time`; without
one, the window is divided by the interval.

```
funding = ±(notional_eur × funding_rate × settlements)     # + = paid, − = received
```

The sign follows the direction: a positive rate means longs pay shorts, so it is a
cost for a long and a **credit** for a short. Both figures are kept, and they say
different things on purpose — `funding_eur` is the honest signed estimate the card
shows, `funding_charged_eur` is `max(0, funding_eur)` and is what net RR spends.
A credit must never be the reason a plan clears `min_rr_tp1`; the behaviour is
switchable via `costs.credit_favourable_funding` so the choice stays visible.

`funding_interval_hours` defaults to **8**, which is Binance's setting for most
USDⓈ-M perps — but some settle every 4h, and a contract at its funding cap drops to
1h. ccxt's premiumIndex response does not carry the interval, so the real per-symbol
value is unavailable without a second API call. This is an estimate on top of an
estimate, and is labelled as one everywhere it is shown.

**Degradation (DATA_SOURCES §4).** A snapshot with no funding rate yields
`funding_available = false` and a zero funding estimate, and the card says
`funding n/a` rather than rendering a reassuring €0.00. Fees are never optional.

**Net reward-to-risk.** Computed on §2.5's `avg_entry` basis and derived from the
*displayed* (2dp) gross multiple, so the two figures on a card reconcile by hand:

```
net_rr_i = (rr_i × risk_eur − entry_fee − tp_exit_fee_i − funding_charged)
           ÷ (risk_eur      + entry_fee + stop_exit_fee + funding_charged)
```

`net_rr ≤ rr` holds by construction for non-negative costs.

**Evaluation order.** The cost check runs **last**, after leverage, the liquidation
buffer and the margin guard. Cost as a share of risk is ≈ `0.07% / stop_distance` —
the same quantity that drives leverage — so any plan failing the margin guard
(notional > 10× capital) necessarily spends >90% of its risk budget on fees.
Gating net RR earlier would make `INSUFFICIENT_MARGIN` and `LIQ_BUFFER` unreachable
and report "thin RR" for a position the owner simply cannot fund.

## 5. Targets & management plan (attached to card)

- TP1/TP2/TP3 from analyst, validated ordered and beyond `min_rr_tp1`.
- Default management text (deterministic template): "TP1: close 40%, move stop to breakeven. TP2: close 35%. TP3: close remainder or trail by 1×ATR." Percentages configurable.
- Time stop: signal EXPIRES if no entry fill within `entry_ttl` (default: 12h for intraday-labeled, 36h for swing-labeled setups).

## 6. Outputs (`TradePlan` additions)

`entries[] {price, weight_pct, qty, notional_eur}`, `avg_entry`, `stop`, `targets[]`, `risk_eur (actual)`, `margin_eur`, `notional_eur`, `suggested_leverage`, `liq_buffer_ok: true`, `rr_tp1/tp2/tp3`, `management_plan`, `expires_at`, `gate_status`.

> **Addition (2026-08-18, from M5.1) — §4.2 costs on the plan.** `schema_version`
> is now **2**. `rr_targets` keeps both its name and its meaning (RR **gross** of
> costs), so plans stored by M4 and M5 remain readable exactly as written; the new
> fields are `rr_targets_net` and a `costs` block carrying `entry_fee_eur`,
> `stop_exit_fee_eur`, `tp_exit_fees_eur[]`, `funding_rate`,
> `funding_interval_hours`, `funding_settlements`, `funding_eur` (signed),
> `funding_charged_eur`, `funding_available`, `round_trip_cost_eur` and
> `cost_pct_of_risk`. The card shows the cost line and both RR figures — the owner
> should see what a trade costs before taking it, not after. The bot renders; it
> never computes (`gate_decisions` stores the whole block as JSONB, no migration).

> **Addition (2026-08-18, from M6) — the distances the card shows.** `schema_version`
> is now **3**. A number the signal card needs and the plan lacks is a gap in this
> spec, not a line to drop from the card: reading `TP1: 85.20` at 3am without a
> percentage means doing mental arithmetic against a moving price. Three additive
> fields, all `Decimal`, all quantized with the same 2dp `percent()` as
> `stop_distance_pct`, so one card never mixes precisions:
>
> * `last_price` — the market price the plan was built against. Stored so the rung
>   percentages stay re-derivable by hand; a figure whose reference price was
>   discarded is not auditable (PRD G5).
> * `target_distances_pct[]` — parallel to `targets`, each
>   `|TP_i − avg_entry| / avg_entry`. Measured from §3's weighted average entry, the
>   same basis as `stop_distance_pct` and every RR figure, so all three reconcile by
>   hand on one card. **Unsigned**: a short's reward is a falling price, and a minus
>   sign in front of it reads as a loss — the card supplies the `+`.
> * `entries[].distance_pct` — `(price_i − last_price) / last_price`. **Signed**,
>   because §2 rule 2 bounds both zone edges within `max_entry_distance_pct` of the
>   last price without forcing the zone to one side of it: a ladder can straddle
>   price, and a renderer inferring the sign from `direction` would then print the
>   wrong one. `stop_distance_pct` is unchanged and stays unsigned.
>
> All three are computed on the **final** ladder, after any min-notional collapse —
> a collapse moves `avg_entry`, and the plan that ships must show the distances of
> the ladder it ships (the same rule §3's collapse ruling applies to rules 3–5).
> Additive only: `gate_decisions.plan` is JSONB, so plans stored under schema 1 and
> 2 stay readable and nothing is backfilled.

## 7. Portfolio rails (checked before every signal AND on every tracker tick)

- Daily realized loss ≥ limit → `PAUSED_LOSS_LIMIT` for 24h; Telegram notice; `/resume` requires explicit confirmation.

> **Ruling (2026-08-18, from M7) — "daily" is the UTC calendar day.** The spec never
> said, and the owner is in Europe/Vilnius. UTC wins because it is the instant every
> stored row and every log line is stamped against, so a pause can always be
> reconciled with the audit trail by eye; the Telegram notice prints local **and**
> UTC like every other timestamp (specs/TELEGRAM_UX.md §6).
>
> Wins and losses net off within the day: §7 limits the *realized* loss, and an
> afternoon that gives most of a bad morning back is not a 3% day. A net profit is
> reported as 0, never as a negative loss. Without `capital_eur` there is no
> denominator and the figure is 0 — the gate already rejects everything with
> `NO_CAPITAL` in that state, so there is nothing a pause would add.
>
> An already-active loss pause is **not re-raised** on the next tick. The tick
> recomputes the day's loss every 60 seconds, so a bad day keeps producing the same
> verdict; re-saving it would slide the 24h window forward forever and re-post the
> §4 notice every minute. `tracker/loop.already_paused_for_loss` is that check.
>
> Dry-run signals are excluded: a paper loss cannot pause a real account.
- `/pause` manual pause any time; pause state persisted in DB (survives restart).
- Capital changes via `/capital` apply to **new** signals only; open signals keep their original sizing (stored, immutable).

## 8. Test plan (write these tests FIRST — TDD for this module)

1. Golden cases: long & short, single-entry & 3-rung, verified by hand-calculated fixtures.
2. Property tests (hypothesis): for random valid inputs — actual risk ≤ configured risk (post-rounding, within 1 qty-step tolerance); liquidation buffer always ≥ 2× stop distance; qty × price ≈ notional.
3. Rejection matrix: one test per coherence rule.
4. Ladder-collapse tests around min-notional boundaries.
5. Partial-fill R accounting: rung1-only + stop = −0.40R (±rounding); rung1+2 + TP1 math correct.
7. **Distances (added 2026-08-18, from M6):** hand-calculated goldens for the rung
   and target percentages on the long baseline and its short mirror; the
   single-entry case; a ladder straddling the last price, proving the rung sign is
   stored rather than derived from `direction`; and recomputation after a collapse
   moves `avg_entry`. Property tests: every stored percentage re-derives from the
   `last_price`/`avg_entry` on the same plan, and target distances are positive and
   ordered with the targets.
8. **Tracker inputs (added 2026-08-18, from M7):** `open_risk_pct`,
   `cooldown_until` and `realized_loss_pct` in `risk/rails.py` — the three
   conversions that build a `PortfolioState` from database rows, which M4 left as a
   plain input. Tests: the risk percentage each open signal was *issued* with
   (§7 keeps open signals on their original sizing); the latest resolution per
   symbol winning regardless of arrival order; a zero-hour cooldown disabling the
   rail rather than pinning every symbol to "expires now"; and the loss-percentage
   cases above. Plus `RejectionReason.DAILY_SIGNAL_CAP` against
   `risk.max_signals_per_day` (specs/TELEGRAM_UX.md §6's cap, which had no config
   key until M7), asserted for its position in the rejection order.

9. **Realized costs (added 2026-08-18, from M7, closing M5.1 §10):**
   `costs.realized_costs_eur` prices the legs that actually happened rather than
   the ones the plan intended — hand-calculated per case on the §8.1 golden ladder:
   a full ladder stopped out costs the plan's estimated €3.16, a rung-1-only
   stop-out costs €0.90, a signal that never filled costs nothing at all, and an
   open position has paid its entry fee and not yet its exit. Funding keeps §4.2's
   sign convention and its `credit_favourable_funding` floor.

10. **Open PnL (added 2026-08-18, from M7):** `accounting.unrealized_pnl_usdt` /
    `unrealized_r` mark the size still held, for `/positions` (specs/TELEGRAM_UX.md
    §3's "live uPnL in R and EUR", deferred at M6 because the math did not exist).
    Measured on the open quantity only, on the same 1R basis as every other figure.

6. **Costs (added 2026-08-18, from M5.1):** hand-calculated fee arithmetic per golden
   case; settlement counting across the expiry window (none / one / many, stale and
   absent `next_funding_time`); the funding sign in both directions; the
   `credit_favourable_funding` floor; the no-funding-rate degrade path. Property
   tests: **net RR ≤ gross RR for every target**, every cost component ≥ 0, and an
   approved plan always clears `min_rr_tp1` on the net figure. The four §8.1 goldens
   as M4 shipped them are retained as `NET_RR_TOO_LOW` rejection cases — the
   behaviour change is asserted, not left to be discovered.
