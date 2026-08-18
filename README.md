# Sentinel — AI Trading Analyst (Spec Pack v1)

A 24/7, self-hosted crypto market-research system: it scans a watchlist, deeply analyzes promising setups with Claude (charts + volume + funding/OI + news + Fear & Greed), applies a deterministic risk engine, and sends complete trade plans to Telegram — EUR position size, leverage, entry ladder, stop, targets. **You approve and execute manually. It never trades by itself.**

This folder is the complete spec pack for building it with **Claude Code** using spec-driven development.

## Contents

```
CLAUDE.md                      ← standing rules for Claude Code (put at repo root)
docs/PRD.md                    ← what we're building and why
docs/ARCHITECTURE.md           ← how it's structured
docs/MILESTONES.md             ← build order M0→M9 with done-criteria
docs/specs/RISK_ENGINE.md      ← sizing, leverage, multi-entry math (TDD)
docs/specs/PROMPTS.md          ← screener + deep analyst prompts & schemas
docs/specs/TELEGRAM_UX.md      ← signal cards, buttons, commands
docs/specs/DATA_SOURCES.md     ← data APIs + accounts shopping list
```

## How to build (the loop)

1. **Prepare accounts** (see `docs/specs/DATA_SOURCES.md` §1): Anthropic API key, Telegram bot token, VPS, CryptoPanic key. No TradingView needed.
2. Create an empty git repo, copy this whole folder into it.
3. Open **Claude Code** at the repo root. First message:
   > "Read CLAUDE.md, docs/PRD.md, docs/ARCHITECTURE.md and docs/MILESTONES.md. Then implement Milestone M0 exactly as specified. Show me your plan first."
4. Review the plan, let it build, run the demo command yourself.
5. Bring the `journal/M0_REPORT.md` + any issues back to the architect chat (your Claude project) for review, get fixes/next instructions, then:
   > "Implement Milestone M1."
6. Repeat through M8, then run the M9 two-week shakedown before risking real money.

Model tip inside Claude Code: use the strongest model (Opus/Fable tier) for milestone planning and the risk engine; Sonnet is fine for routine implementation.

## Non-negotiables

- No execution code, no trade-enabled API keys — v1 is signals + tracking only.
- The LLM analyzes; deterministic code sizes and limits; a human decides.
- Every signal outcome is tracked so the real success rate is measured, not assumed.

*Personal research tool. Not financial advice. Past statistics do not guarantee future results.*
