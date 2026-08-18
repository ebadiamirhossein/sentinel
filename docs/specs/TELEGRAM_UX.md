# SPEC — Telegram Bot UX

**Module:** `sentinel/bot/` (aiogram 3). Single authorized user (Telegram user-id allowlist in `.env`; all other users get silence).

---

## 1. Signal card (the core message)

```
🟢 LONG — SOLUSDT   [trend_pullback · intraday · conf 78]
Prompt v3 · Signal #142 · 2026-08-17 14:32 UTC

📊 Thesis
4h uptrend intact; 1h pullback into EMA50 + prior breakout level
82.4 with volume drying up on the retrace; funding neutral,
OI rising with price. 15m shows demand absorption at zone top.

⚠️ Against: 24h F&G at 74 (greed) — crowded-long risk.

🎯 Plan (capital €10,000 · risk 0.75% = €75)
Entry ladder (limit orders):
  1) 83.10 — 40% — 0.481 SOL (€40 margin share)
  2) 82.60 — 35% — 0.423 SOL
  3) 82.10 — 25% — 0.304 SOL
  Avg entry: 82.68

🛑 Stop: 81.20 (−1.79%)  |  ❌ Invalidation: 1h close < 81.40
🥅 TP1: 84.90 (+2.7% · 1.5R · close 40%, stop→BE)
🥅 TP2: 86.60 (+4.7% · 2.6R · close 35%)
🥅 TP3: 88.90 (+7.5% · 4.2R · trail 1×ATR)

💶 Notional: €4,190  |  Margin: €1,000  |  Leverage: 4x (isolated)
✅ Liq. buffer OK (liq ≈ −25% vs stop −1.79%)
⏳ Expires if unfilled: 12h

[✅ Taken]  [👀 Watching]  [❌ Skip]
```

Every number comes from `TradePlan` — the bot renders, never computes. Charts (1h + 4h PNGs) attached as an album above the card.

## 2. Buttons & effects

| Button | Effect |
|---|---|
| ✅ Taken | signal → ACTIVE; counts toward open-risk budget and **real** stats; tracker posts fill/TP/SL updates as replies |
| 👀 Watching | tracker follows outcome for **hypothetical** stats only; no risk budget used |
| ❌ Skip | archived; outcome still resolved in background for hypothetical stats (measures what skipping costs/saves) |

After entry fills, ACTIVE signals gain a second row: `[🔚 Closed manually] [✏️ Note]` — manual close asks for exit price to record honest realized R.

## 3. Commands

| Command | Behavior |
|---|---|
| `/capital 10000` | Set capital EUR (validated > 0); confirms; applies to new signals only |
| `/risk 0.75` | Set per-trade risk % (0.25–1.5 enforced) |
| `/status` | Pipeline health: last cycle time, data sources OK/degraded, active signals, open risk used, paused? |
| `/positions` | All ACTIVE signals with live uPnL in R and EUR |
| `/stats [30d|90d|all]` | Real stats (taken): count, win rate, avg R, profit factor, max DD; then hypothetical (watched/skipped); breakdown by setup_type and prompt version |
| `/pulse` | On-demand market regime summary (P1) |
| `/analyze SOLUSDT` | Force deep analysis of one symbol now (P1) |
| `/watchlist [add|remove SYMBOL]` | View/edit watchlist |
| `/pause` / `/resume` | Manual pause; resume from loss-limit pause requires confirming button "Yes, resume" |
| `/settings` | Show all runtime config values |

## 4. Tracker notifications (replies to the original card)

- `📥 Entry 1 filled @ 83.10 (40%)` … `📥 Ladder complete, avg 82.68`
- `🎯 TP1 hit @ 84.90 → close 40%, stop moved to BE (per plan)`
- `🛑 Stopped @ 81.20 → −0.72R (partial ladder: only rungs 1–2 filled) · −€54`
- `❌ Invalidation triggered (1h close 81.05 < 81.40) before entry → signal cancelled`
- `⌛ Expired unfilled after 12h`
- `⏸️ Daily loss limit reached (−3.1%). New signals paused 24h. /resume to override.`

## 5. Digest (P1)

Daily 08:00 (owner timezone, config): yesterday's signals & outcomes, running week stats, market regime line, any degraded data sources.

## 6. Non-functional

- All messages idempotent (message ids stored; restarts never double-post).
- No signal spam: hard cap `max_signals_per_day` (default 5) and per-symbol cooldown.
- Language: English v1 (Farsi toggle listed as P2).
- Every card footer: `Research tool — not financial advice. Past stats ≠ future results.`
