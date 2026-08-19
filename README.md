# Sentinel — AI Trading Analyst

A 24/7, self-hosted crypto market-research system. It scans a watchlist, deeply
analyses promising setups with Claude (charts + volume + funding/OI + news + Fear
& Greed), applies a deterministic risk engine, and sends complete trade plans to
Telegram — EUR position size, leverage, entry ladder, stop, targets. Then it
tracks what actually happened to every signal, so the success rate is measured
rather than assumed.

**You approve and execute manually. It never trades by itself.** There is no
execution code, and no exchange credential with trade or withdraw permission
exists anywhere in this repository.

---

## How it works

```
every 15 min ─▶ ingestion ─▶ features ─▶ screener ─▶ deep analyst ─▶ risk gate ─▶ Telegram
                 (ccxt,       (EMA/RSI/   (cheap      (Fable 5,      (math,        (card +
                  news, F&G)   ATR, S/R)   LLM)        + charts)      no LLM)       buttons)
                                                                          │
every 60s   ─▶ tracker ─▶ fills / stops / targets / invalidation ─▶ R accounting ─▶ /stats
```

The division of labour is the whole design: **the LLM only produces judgment.**
Everything that must be exact, testable and repeatable — indicators, sizing,
leverage, portfolio limits, outcome tracking — is deterministic code with tests.
The analyst never sizes, never sets leverage and never approves; the risk gate
outputs the numbers, and a human decides.

## Status

| Milestone | | |
|---|---|---|
| M0 | Scaffold, Compose, migrations | ✅ |
| M1 | Ingestion + snapshot assembler | ✅ |
| M2 | Feature engine | ✅ |
| M3 | Chart renderer | ✅ |
| M4 | Risk engine (100% branch coverage) | ✅ |
| M5 | LLM pipeline (screener + analyst) | ✅ |
| M6 | Telegram bot | ✅ |
| M7 | Orchestrator, tracker, stats, spend guard | ✅ |
| M8 | Hardening & deploy | ✅ |
| M9 | Two-week shakedown, signals only | ← next |

## Running it on a server

**→ [docs/DEPLOY.md](docs/DEPLOY.md)** — fresh server to running system in under 30
minutes, including backups with a restore drill, failure alerts, log rotation, what
happens on reboot, and how to ship the next commit.

The short version:

```bash
git clone <repo> /opt/sentinel && cd /opt/sentinel
cp .env.example .env && nano .env      # keys, Telegram token, allowlist
docker compose up -d --build
curl -fsS localhost:18080/health
```

Start with `dry_run: true` in `config.yaml` for the first 24 hours: the whole
pipeline runs and publishes nothing, so the first unattended day produces a
measured paper record instead of merely an absence of crashes.

## Working on it locally

```bash
make install        # .venv with dev extras
make up             # app + postgres (publishes Postgres on 127.0.0.1 for host tooling)
make check          # tests, ruff, mypy --strict, risk coverage, ops scripts, wheel, image
```

`make check` is the milestone gate and none of its steps skip silently. Useful
demos, each hitting real data:

```bash
python -m sentinel.tools.snapshot BTCUSDT     # a full validated market snapshot
python -m sentinel.tools.size --fixture examples/sol_long.json
python -m sentinel.tools.cycle --once --dry-run
python -m sentinel.tools.track --once
python -m sentinel.tools.stats
python -m sentinel.tools.spend                # what the LLM has cost today
python -m sentinel.tools.alert --simulate 3   # what an admin alert looks like
```

## The documents

```
CLAUDE.md                      ← standing rules for Claude Code
docs/PRD.md                    ← what we're building and why
docs/ARCHITECTURE.md           ← how it's structured
docs/MILESTONES.md             ← build order with done-criteria
docs/DEPLOY.md                 ← the runbook
docs/specs/RISK_ENGINE.md      ← sizing, leverage, multi-entry math (TDD)
docs/specs/PROMPTS.md          ← screener + deep analyst prompts & schemas
docs/specs/TELEGRAM_UX.md      ← signal cards, buttons, commands
docs/specs/DATA_SOURCES.md     ← data APIs + accounts shopping list
docs/specs/ENSEMBLE.md         ← the second opinion (M10)
journal/                       ← one report per milestone: decisions, deviations, evidence
```

Specs are the source of truth. Where code and spec disagree, the spec wins; where
a spec turned out to be wrong, the correction is dated in the spec itself and
explained in the milestone's journal entry.

## Non-negotiables

- No execution code, no trade-enabled API keys — v1 is signals and tracking only.
- The LLM analyses; deterministic code sizes and limits; a human decides.
- Every signal outcome is tracked, so the real success rate is measured.
- All money is `Decimal`, all timestamps are UTC, `mypy --strict` is clean.

*Personal research tool. Not financial advice. Past statistics do not guarantee
future results.*
