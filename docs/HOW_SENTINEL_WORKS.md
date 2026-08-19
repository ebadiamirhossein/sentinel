# How Sentinel Works

A plain-language explanation of the AI trading research system — what it does, how it decides, and what it deliberately does not do.

---

## 1. What it is in one paragraph

Sentinel watches a list of crypto perpetual futures markets around the clock. Once an hour it gathers data on every symbol, cheaply triages which ones deserve attention, and sends the interesting ones to a strong AI model along with chart images. If the AI finds a genuine setup, a piece of deterministic code turns that idea into a complete trade plan — exact entry prices, position sizes in euros, stop loss, targets, and suggested leverage — and sends it to Telegram. **A human reads it and places the order by hand.** The system has no exchange keys and cannot trade. It then follows every signal it produced to its conclusion and records whether it was right.

**The core philosophy:** the AI does judgment. Code does arithmetic. A human makes the decision.

---

## 2. What it is not

- **It is not an auto-trader.** There is no order-placing code anywhere in the repository, and no API key with trade permission exists. This is a deliberate, enforced constraint, not a missing feature.
- **It is not a proven strategy.** As of now it has produced zero measured signals. Its win rate is unknown. Anyone using it is testing it, not profiting from it.
- **It is not financial advice.** It is a research tool that produces trade ideas for a human to evaluate.

---

## 3. The pipeline — what happens every hour

```
1. INGEST      Fetch market data for all watchlist symbols
2. FEATURES    Compute indicators with plain math (no AI)
3. SCREEN      Cheap AI model picks 0–3 symbols worth deep analysis
4. CHART       Render candlestick images for those symbols
5. ANALYSE     Strong AI model reads charts + data, forms a thesis
6. RISK GATE   Deterministic code sizes it, or rejects it
7. DELIVER     Complete trade plan to Telegram
8. TRACK       Follow the outcome every 60 seconds until resolved
```

### Step 1 — Ingest

For each symbol, the system fetches from Binance's public endpoints (read-only, no account required):

- **Price candles** at four timeframes: 15-minute, 1-hour, 4-hour, daily
- **Volume**
- **Order book depth** — the buy/sell imbalance in the top 50 levels
- **Funding rate** — what longs pay shorts (or vice versa); reveals crowding
- **Open interest** — how much total money is in positions, and its 24h change
- **Long/short ratio** of top traders

Plus market-wide context: the **Fear & Greed index**, **Bitcoin dominance**, and **news headlines** (via CryptoPanic or RSS).

Every piece of data is timestamped. If something is stale or missing, the snapshot is marked `DEGRADED` and the AI is told exactly what is missing — it never gets a fabricated value.

### Step 2 — Features (pure math, no AI)

Code computes, with no AI involvement:

- **EMA 20 / 50 / 200** — moving averages showing trend direction
- **RSI(14)** — momentum, whether something is overbought or oversold
- **ATR(14)** — how much this market typically moves, used for sizing stops
- **Relative volume** — is today's volume unusual compared to normal?
- **Trend regime** per timeframe — UPTREND / DOWNTREND / RANGING / TRANSITIONING, based on the EMA stack, the slope of the EMA50, and where price sits
- **Support and resistance levels** — found by detecting pivot highs and lows, then clustering nearby ones into zones and scoring them by how many times price touched them and how recently

These are exact, testable, and reproducible. The same data always produces the same numbers.

### Step 3 — Screen (cheap AI)

All symbols go to a fast, inexpensive model in a **single batched call** (about 2 cents total). It sees only compact numbers — price changes, RSI, EMA relationships, volume, funding, open interest change, Fear & Greed, distance to the nearest support/resistance, and whether fresh news exists.

Its only job: **which symbols deserve expensive attention right now?**

It is instructed that "nothing is happening" is the expected answer for most symbols most of the time, and that flagging many symbols is a failure. It typically returns 0–3.

### Step 4 — Charts

For each flagged symbol, the system draws its own candlestick charts (it does not screenshot TradingView) at 15m, 1h and 4h. Each chart shows candles, the three EMAs, a volume panel, marked support/resistance lines with prices, and a timestamp watermark.

These images are drawn from stored data, so any historical chart can be reproduced exactly.

### Step 5 — Analyse (the strong AI, with vision)

This is the expensive step (~28 cents per symbol). The AI receives the full data snapshot as text **and the three chart images**, which it reads visually the way a person would.

**This is where the strategy lives — see section 4 below.**

Its output is strictly structured: a thesis, the setup type, direction, an entry zone, a stop, up to three targets, an invalidation condition, a counter-argument against its own idea, a confidence score 0–100, and one of three verdicts:

- **CANDIDATE** — worth sizing and reviewing
- **WATCHLIST** — interesting but not actionable
- **NO_SETUP** — nothing here

Critically: **the AI never chooses position size, leverage, or euro amounts.** It produces market judgment only.

### Step 6 — The risk gate (pure math, no AI)

Deterministic code takes the AI's idea and either rejects it or turns it into an exact plan. See section 5.

### Step 7 — Deliver

A Telegram card with everything needed to place the trade in under two minutes, plus three buttons: ✅ Taken / 👀 Watching / ❌ Skip.

### Step 8 — Track

Every 60 seconds, code checks 1-minute candles for each open signal and detects: each entry filling, the stop being hit, each target being reached, invalidation, or expiry. It posts updates as replies under the original card and records the outcome.

---

## 4. The strategy — how it actually decides

**There is no single strategy like "buy when RSI is below 30."** The system works like a disciplined trader with a checklist. Three things must line up: the chart structure, the supporting evidence, and the absence of a good counter-argument.

### 4.1 The five allowed setups

The AI must fit what it sees into exactly one of these. If nothing fits cleanly, the answer is NO_SETUP.

| Setup | In plain words |
|---|---|
| **Trend pullback** | Price is in a clear trend, dips back to a key level (like the EMA50 or a prior breakout point), and is likely to continue. Buy the dip in an uptrend. |
| **Range reversal** | Price is bouncing between a floor and a ceiling. Buy near the floor, sell near the ceiling. |
| **Breakout retest** | Price broke through a level, came back to touch it from the other side, and held. Enter on the retest — **never on the breakout candle itself.** |
| **Momentum continuation** | A strong move with strong volume behind it, likely to keep going. |
| **Mean reversion** | Price moved too far too fast and is likely to snap back. |

### 4.2 The three-timeframe protocol

The AI examines the charts in a fixed order, and each timeframe answers a different question:

1. **4-hour — which direction is allowed?** Is the market trending or ranging? Where are the major levels? This sets the context and keeps the system out of counter-trend trades.
2. **1-hour — is there a setup?** This is where the structure must fit one of the five types above.
3. **15-minute — is the timing right?** Where exactly should the entry sit?

### 4.3 The confluence check

A chart pattern alone is never enough. The idea must also survive:

- **Volume** — does volume confirm the move, or contradict it?
- **Funding rate and open interest** — is the trade already crowded? If everyone is long and paying to stay long, a long is more dangerous.
- **Fear & Greed** — is sentiment at an extreme that argues against the setup?
- **News** — is there fresh news that could invalidate the technical picture?

### 4.4 The liquidity trap check

Before finalising, the AI must ask: **where are everyone else's stop losses sitting?** Equal highs and lows, round numbers, obvious swing points. It must assume those pools may be swept before the real move, and place the stop *beyond* the likely sweep zone rather than inside it.

If a safe stop placement makes the risk/reward too poor, the correct answer is NO_SETUP — not a tighter stop.

This is also why invalidation is defined as a **candle close**, not a wick. A 30-second spike through a level is noise; a full hourly candle closing through it is not.

### 4.5 The counter-thesis requirement

The AI must write the strongest argument *against* its own trade. If it cannot refute that argument with evidence it can cite, the idea is downgraded one level (CANDIDATE → WATCHLIST → NO_SETUP).

### 4.6 Evidence citation

Every claim in the thesis must point at the specific data field or chart observation that supports it. The AI is forbidden from inventing a price, level, indicator value, or news item. If data is missing or stale, it must say so rather than fill the gap.

### 4.7 The bias toward silence

The instruction is explicit: **a forced setup is the worst possible failure.** NO_SETUP is always preferable to a weak idea. The system is designed to say nothing most of the time.

---

## 5. The risk engine — where the numbers come from

This module contains no AI at all. It is pure arithmetic, tested to 100% branch coverage, using exact decimal math.

### 5.1 Rejection checks

An idea is rejected outright if any of these fail:

- The stop is on the wrong side of the entry, or targets are out of order
- The entry zone is more than 3% away from current price (no chasing)
- The stop is tighter than 0.6× ATR (guaranteed to be stopped out by noise)
- The stop is wider than 3× ATR (lazy, wrecks the risk/reward)
- The **net** reward-to-risk at the first target is below 1.5
- Confidence is below 60
- Portfolio limits are exceeded: too much open risk, too many positions, symbol on cooldown, daily loss limit hit, or paused

### 5.2 The entry ladder

If the entry zone is narrow, there is a single entry. If it is wide (half an ATR or more), the plan uses **three limit orders** at 40% / 35% / 25% of the risk budget, spread across the zone.

This matters for two reasons. It produces a better average entry price on pullbacks. And when the market sweeps down to hunt stops, the lower rungs get filled at better prices — the sweep works *for* you instead of against you.

Partial fills are accounted honestly: if only the first rung fills and the stop is hit, the loss recorded is 0.4R, not 1R.

### 5.3 Sizing and leverage

```
risk in euros  = capital × risk %          (e.g. €10,000 × 0.75% = €75)
notional       = risk ÷ stop distance %
quantity       = notional ÷ entry price    (rounded down to exchange steps)
leverage       = notional ÷ margin budget  (capped, then reduced if needed)
```

**Leverage is derived, never chosen.** It is capped at 10×, and then reduced further until the liquidation price sits at least twice as far away as the stop. This guarantees a planned stop-out can never liquidate the position.

**Leverage changes how much margin you post — not how much you can lose.** The loss is fixed by the stop and the risk percentage.

### 5.4 Costs

Trading fees and funding are subtracted before the trade is approved. Entries are priced as maker fees (0.02%), every exit as taker (0.05%), and funding is estimated over the expected holding window. The card shows both **gross** and **net** reward-to-risk, and **the approval threshold is applied to the net figure**.

This matters more than it sounds: with a tight stop, fees can eat 15% of the risk budget. Ignoring them would have made every measured statistic look better than reality.

---

## 6. What a signal looks like

```
🟢 LONG — SOLUSDT   [trend_pullback · intraday · conf 78]

📊 Thesis
4h uptrend intact; 1h pullback into EMA50 and the prior breakout
level 82.4 with volume drying up on the retrace. Funding neutral,
OI rising with price. 15m shows demand absorption at the zone top.

⚠️ Against: Fear & Greed at 74 (greed) — crowded-long risk.

🎯 Plan (capital €10000 · risk 0.75% = €75.00)
Entry ladder (limit orders) — last price 83.40:
  1) 83.10 (-0.36%) — 40% of risk — 18.30 SOL
  2) 82.60 (-0.96%) — 35% of risk — 21.73 SOL
  3) 82.10 (-1.56%) — 25% of risk — 24.15 SOL

🛑 Stop: 81.20 (-1.78%)
❌ Invalidation: 1h close below 81.40
🥅 TP1: 85.20 (+3.05%) — 1.59R net (1.71R gross)
🥅 TP2: 86.60 (+4.75%) — 2.50R net
🥅 TP3: 88.90 (+7.53%) — 3.99R net

💶 Notional €4570 · Margin €914 · Leverage 5x (isolated)
🧾 Costs: round trip €3.34 = 4.45% of the risk budget
✅ Liq. buffer OK (liq ≈ 20% vs stop 1.78%)
⏳ Expires if unfilled: in 12h

[✅ Taken]  [👀 Watching]  [❌ Skip]
```

---

## 7. Measurement — the part that matters most

**Every signal is tracked to its conclusion, regardless of which button you press.** The buttons decide which column the result lands in:

- **✅ Taken** → your **REAL** record. Real money was at risk.
- **👀 Watching / ❌ Skip** → **HYPOTHETICAL**. Followed and measured identically, but nothing was at stake. This tells you what skipping cost or saved you.

`/stats` reports, for each population: number of signals, win rate, average R, profit factor, maximum drawdown, and a breakdown by setup type and by prompt version.

**Why the prompt-version breakdown matters:** the AI's instructions are stored as versioned files. When they change, the version is recorded on every signal. So after enough data you can answer "did version 2 actually produce better trades than version 1?" with evidence rather than opinion. That is the improvement loop the whole design is built around.

---

## 8. Safety limits

| Limit | Default | What it does |
|---|---|---|
| Risk per trade | 0.75% | Maximum loss on one trade |
| Max open risk | 2.25% | Total risk across all open positions |
| Max positions | 4 | Concurrent open signals |
| Max signals/day | 5 | Hard cap on volume |
| Daily loss limit | 3% | Auto-pauses for 24 hours if hit |
| Symbol cooldown | 4h | No new signal on a symbol just resolved |
| Max leverage | 10× | Hard ceiling |
| Liquidation buffer | 2× | Liquidation must be twice as far as the stop |
| LLM spend limit | $10/day | Suspends analysis; tracking keeps running |

All of these are enforced in code, not in the AI's instructions — the AI cannot talk its way past them.

---

## 9. Technical stack

- **Python 3.12**, fully type-checked, ~1,200 automated tests
- **PostgreSQL** stores every snapshot, AI call, signal, fill and outcome — any signal can be reconstructed from inputs to result
- **Docker Compose**, running 24/7 on a small server
- **Anthropic API** — a cheap model for screening, Fable 5 for deep analysis
- **Telegram** — the entire user interface
- Built with spec-driven development: full specification documents written before code, milestone by milestone with review gates

---

## 10. Honest current status

- The system is **live but in dry-run mode** — it runs the full pipeline and publishes nothing while behaviour is observed.
- **Zero signals have been measured.** The win rate is completely unknown.
- Several genuine bugs were found by running it against real markets that no test caught — including fee-blind reward calculations, an entire dead admin command surface, and an analysis loop that re-analysed the same market condition every 15 minutes.
- A planned second-opinion layer (a second AI model that can veto but never create a signal) exists in the specification but is not built yet.
- Forex is designed for but not implemented — the data interface exists, no forex adapter does.

**The honest summary:** this is a carefully-built measurement instrument. Whether the thing it measures is profitable is exactly the question it was built to answer, and that answer does not exist yet.

---

*Research tool — not financial advice. Past statistics do not guarantee future results. Every order is placed manually by a human.*
