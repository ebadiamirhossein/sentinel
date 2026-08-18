# PRD — "Sentinel" AI Trading Analyst (v1)

**Owner:** You (single user, self-hosted)
**Status:** Approved for build
**Build method:** Spec-driven development with Claude Code
**Runtime:** Docker Compose on a 24/7 VPS

---

## 1. Problem Statement

Manually scanning crypto markets across timeframes, news, volume, funding, and sentiment is slow, emotional, and inconsistent. The owner wants a 24/7 autonomous research system that continuously scans the market, deeply analyzes promising setups with an LLM (charts + data + news + sentiment), applies deterministic risk math, and delivers a complete, actionable trade plan to Telegram — including exact EUR position size, suggested leverage, laddered entries, stop, and targets — which the owner approves and executes manually.

## 2. Goals

1. **G1 — Complete signals:** Every signal delivered to Telegram is fully actionable with zero extra work: direction, entry ladder (1–3 entries with prices and EUR sizes), stop, 2–3 targets, suggested leverage, margin in EUR, and invalidation condition.
2. **G2 — Measured performance:** Every signal's outcome (TP/SL/expired) is tracked automatically. Win rate, average R, profit factor, and max drawdown are computable at any time via `/stats`. The system's real success rate is *known*, not guessed.
3. **G3 — High signal quality over quantity:** The system prefers NO_SETUP over weak setups. Target: ≤ 5 signals/day across the watchlist; ≥ 60% of signals should reach at least TP1 or breakeven after 60 days of tuning (stretch: 65%). Note: this is a system target for iteration, not a guaranteed outcome.
4. **G4 — Reliability:** ≥ 99% scan-cycle completion over 30 days; no missed cycles longer than 2 consecutive intervals; automatic recovery after crashes.
5. **G5 — Full auditability:** Every LLM input snapshot, output, risk-gate decision, and user action is stored in Postgres and reconstructable.

## 3. Non-Goals (v1)

- **No trade execution.** No exchange write-access API keys anywhere in the codebase. Owner executes manually. (Reason: measure real performance before automating money.)
- **No forex or stocks in v1.** Architecture is asset-agnostic (data adapters), but only the crypto adapter ships. (Reason: free 24/7 data, richer signals; forex is a v2 adapter.)
- **No web dashboard in v1.** Telegram is the only UI. (Reason: fastest path to daily use; dashboard is P2.)
- **No ML model training / fine-tuning.** Prompt + deterministic features only. (Reason: not enough labeled outcome data yet — the tracker will create that data.)
- **No portfolio management / rebalancing.** One trade plan at a time per symbol.

## 4. Users & Stories

Single persona: the **owner-trader** (executes manually on their exchange, EUR-denominated capital).

- As the owner, I want the system to scan my watchlist 24/7 so that I never miss a 1–4h setup while asleep or working.
- As the owner, I want each signal to state EUR margin, notional, and leverage computed from my configured total capital so that I can place the trade in under 2 minutes without any math.
- As the owner, I want multi-entry ladders when the setup benefits from scaling in, so that I get a better average entry in pullback setups.
- As the owner, I want to set my capital (`/capital 10000`) and risk (`/risk 0.75`) in Telegram so that sizing always reflects reality.
- As the owner, I want to mark a signal ✅ Taken / ❌ Skipped / 👀 Watching so the tracker knows which outcomes count toward my real stats vs. hypothetical stats.
- As the owner, I want automatic follow-up messages when price hits an entry, TP, SL, or invalidation on a taken signal, so I can manage the position.
- As the owner, I want `/stats` (win rate, avg R, profit factor, by-setup breakdown) so I can see what's actually working and tune the system.
- As the owner, I want the system to pause itself after hitting the daily loss limit so I'm protected from tilt/cascade days.
- As the owner, I want a NO_SETUP quiet mode (no spam) with an optional `/pulse` command to get the current market read on demand.

## 5. Functional Requirements

### P0 — Must have

| ID | Requirement | Acceptance criteria (summary) |
|----|-------------|-------------------------------|
| F1 | Scheduled scan cycle (default every 15 min; deep analysis primarily on 1h/4h structure with 15m timing) | Cycle completes < 5 min for 12 symbols; skipped/failed symbols logged, never block others |
| F2 | Data ingestion: OHLCV (15m/1h/4h/1D), volume, order book depth snapshot, funding rate, open interest, long/short ratio, Fear & Greed index, BTC dominance, news headlines | Every record has `timestamp` + `source`; stale data (> 2× its refresh interval) flags the snapshot as degraded |
| F3 | Deterministic feature engine: EMA(20/50/200), RSI(14), ATR(14), relative volume, higher-timeframe trend regime, support/resistance levels, volatility regime | Pure functions, unit-tested against known fixtures; no LLM involvement |
| F4 | Chart renderer: candlestick PNGs (15m, 1h, 4h) with EMAs, volume, and marked S/R levels, fed to the analyst as vision input | Charts reproducible from stored OHLCV; image + params stored per signal |
| F5 | Two-tier LLM pipeline: cheap screener (Sonnet-class) filters all symbols → deep analyst (Fable 5-class) analyzes only candidates with charts + full context | Screener returns strict JSON; analyst returns schema-validated JSON; retry once on invalid JSON then discard with log |
| F6 | Analyst output: thesis, setup type, direction, entry zone, stop, targets, invalidation, confidence 0–100, `candidate_status ∈ {CANDIDATE, WATCHLIST, NO_SETUP}` | Analyst never sizes positions, never sets leverage, never invents data; numeric coherence checked downstream |
| F7 | Deterministic risk gate (see `specs/RISK_ENGINE.md`): converts CANDIDATE → sized plan with EUR amounts, leverage, entry ladder; or rejects | 100% unit-test coverage on sizing math; LLM output that fails coherence checks is rejected with a stored reason |
| F8 | Telegram bot: signal cards, inline buttons (Taken/Skip/Watch), commands `/capital /risk /status /stats /pause /resume /pulse /positions /watchlist` | See `specs/TELEGRAM_UX.md`; all state changes persisted |
| F9 | Outcome tracker: monitors open/taken signals against live price; detects entry fills, TP hits, SL hits, invalidation, expiry; posts updates; computes stats | R accounting handles partial ladder fills correctly |
| F10 | Persistence & audit: Postgres stores snapshots, LLM I/O, signals, fills, outcomes, config changes | A signal can be fully reconstructed (inputs → analysis → sizing → outcome) from DB alone |
| F11 | Safety rails: daily loss limit (default −3% realized R-equivalent → auto-pause 24h), max concurrent open risk (default 3 × per-trade risk), max 1 active signal per symbol | Rails enforced in code, not prompts; pause state survives restart |
| F12 | Ops: Docker Compose deploy, `.env` secrets, structured logging, healthcheck, crash restart, Telegram alert on repeated failures | `docker compose up -d` on a fresh VPS reaches healthy state with only `.env` edited |

### P1 — Should have (fast follow)

- Daily digest message (last 24h: signals, outcomes, market regime summary).
- Weekly performance report with per-setup-type breakdown.
- News event guard: suppress new signals ±30 min around major scheduled events (FOMC/CPI) via economic calendar feed.
- `/analyze BTCUSDT` on-demand deep analysis of any symbol.
- Backtest harness replaying stored snapshots through the pipeline for prompt iteration.

### P2 — Future (design for, don't build)

- Forex data adapter (majors) behind the same interface.
- Semi-auto execution (place order after Telegram approval) behind a hard feature flag, disabled by default.
- Web dashboard (read-only) over the same Postgres.
- Learning loop: feed per-setup stats back into analyst context ("your breakout setups on low-volume alts have 30% win rate — be stricter").

## 6. Success Metrics

**Leading (first 2–4 weeks):** cycle reliability ≥ 99%; JSON-validity of LLM outputs ≥ 98%; signal completeness (every card has all fields) = 100%; owner time-to-execute < 2 min.
**Lagging (30–90 days):** tracked win rate and average R trending up across prompt iterations; profit factor > 1.3 on taken signals (stretch 1.6); ≥ 70% of NO_SETUP days confirmed as correctly quiet (no obvious missed A+ setups in review).

## 7. Constraints & Decisions

- **Market:** Crypto perpetual futures data (Binance as primary source), watchlist default: BTC, ETH, SOL, BNB, XRP, DOGE, AVAX, LINK, LTC, ADA (configurable).
- **Base currency:** EUR for all sizing display (price data in USDT; ECB/exchange EURUSD rate fetched for conversion, cached 1h).
- **Timeframes:** 4h = context/regime, 1h = primary setup TF, 15m = entry timing. The analyst may label a setup as intraday or swing per market conditions.
- **Cost posture:** Budget not a constraint → deep analysis uses the strongest available model with high reasoning effort; screener stays cheap for latency.
- **Legal/positioning:** This is a personal research/decision-support tool. It must never claim guaranteed returns; every stats output includes realized historical figures only.

## 8. Open Questions (non-blocking)

- Which exchange will the owner execute on? (Affects leverage step rounding and min-notional checks — default to Binance conventions until confirmed.) → **Owner**
- Preferred quiet hours for non-urgent messages? → **Owner**
- CryptoPanic free tier vs. paid news source after v1 evaluation. → **Owner, after 2 weeks of use**
