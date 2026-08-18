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
| `min_rr_tp1` | 1.5 | Reject plans below this |
| `eurusd_rate` | fetched hourly, cached | For USDT→EUR display conversion |

From the analyst (`AnalystReport`): direction, entry_zone {low, high}, stop, targets[], setup_type, confidence.

## 2. Coherence checks (reject with reason if any fail)

1. Long: `stop < entry_zone.low < entry_zone.high` and all targets > entry_zone.high. Short: mirrored.
2. Entry zone within `max_entry_distance_pct` (default 3%) of last price — no chasing, no fantasy fills.
3. Stop distance ≥ 0.6 × ATR(14, 1h) — rejects noise-level stops that guarantee stop-outs.
4. Stop distance ≤ 3 × ATR(14, 1h) — rejects lazy wide stops that wreck RR.
5. RR to TP1 (from weighted avg entry) ≥ `min_rr_tp1`.
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

**Liquidation buffer rule:** approximate isolated-margin liquidation distance ≈ `1/leverage` (conservative, ignoring maintenance margin tiers in v1). Require:

```
liq_distance_pct ≥ 2.0 × stop_dist_pct
```

If violated, reduce leverage until satisfied; if leverage would fall below 1, reject. This guarantees the stop always triggers far before liquidation — the position can never be liquidated on a planned stop-out.

**Min notional rule:** each rung's notional ≥ `max(exchange_minimum, 20 USDT)`. If a rung falls below, collapse to fewer rungs (3→2→1) preserving total notional.

> **Correction (2026-08-18, from M1):** the earlier "Binance ≈ 5 USDT" was wrong — the exchange minimum is **per symbol**, not a global constant, and it can exceed the 20 USDT practicality floor. Measured live via `ccxt` `limits.cost.min` on Binance USDT-M: **BTCUSDT = 50 USDT**, **SOLUSDT = 5 USDT**. So the rule is a `max()`, never a fixed 20: the engine must read `InstrumentMeta.min_notional` (ingested and cached per symbol since M1) and take the larger of it and the 20 USDT floor. Hardcoding 20 would produce rungs Binance rejects on BTCUSDT.

**Rounding:** qty down to symbol qty-step; prices to symbol tick-size (fetched from exchange info, cached daily). After rounding, recompute actual risk_eur and display the real number.

## 5. Targets & management plan (attached to card)

- TP1/TP2/TP3 from analyst, validated ordered and beyond `min_rr_tp1`.
- Default management text (deterministic template): "TP1: close 40%, move stop to breakeven. TP2: close 35%. TP3: close remainder or trail by 1×ATR." Percentages configurable.
- Time stop: signal EXPIRES if no entry fill within `entry_ttl` (default: 12h for intraday-labeled, 36h for swing-labeled setups).

## 6. Outputs (`TradePlan` additions)

`entries[] {price, weight_pct, qty, notional_eur}`, `avg_entry`, `stop`, `targets[]`, `risk_eur (actual)`, `margin_eur`, `notional_eur`, `suggested_leverage`, `liq_buffer_ok: true`, `rr_tp1/tp2/tp3`, `management_plan`, `expires_at`, `gate_status`.

## 7. Portfolio rails (checked before every signal AND on every tracker tick)

- Daily realized loss ≥ limit → `PAUSED_LOSS_LIMIT` for 24h; Telegram notice; `/resume` requires explicit confirmation.
- `/pause` manual pause any time; pause state persisted in DB (survives restart).
- Capital changes via `/capital` apply to **new** signals only; open signals keep their original sizing (stored, immutable).

## 8. Test plan (write these tests FIRST — TDD for this module)

1. Golden cases: long & short, single-entry & 3-rung, verified by hand-calculated fixtures.
2. Property tests (hypothesis): for random valid inputs — actual risk ≤ configured risk (post-rounding, within 1 qty-step tolerance); liquidation buffer always ≥ 2× stop distance; qty × price ≈ notional.
3. Rejection matrix: one test per coherence rule.
4. Ladder-collapse tests around min-notional boundaries.
5. Partial-fill R accounting: rung1-only + stop = −0.40R (±rounding); rung1+2 + TP1 math correct.
