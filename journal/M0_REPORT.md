# M0 — Scaffold & Foundations · Report

**Date:** 2026-08-18
**Status:** DONE — `make test lint typecheck` green (47 tests), `docker compose up` → `/health` ok.

---

## 1. What was built

| Area | Delivered |
|---|---|
| Repo layout | Full module tree from ARCHITECTURE.md §3: `ingestion/adapters`, `features`, `charts`, `screener`, `analyst/prompts`, `risk`, `bot`, `tracker`, `stats`, `storage`, `core`, `tools`, `main.py`. Future modules are documented placeholders. |
| Packaging | `pyproject.toml`, Python `>=3.12,<3.13`, hatchling. Ruff + mypy(strict) + pytest config co-located. |
| Config | `sentinel/core/config.py`: `Secrets` (env/`.env`, `SecretStr`) + `AppConfig` (yaml). `load_config()` implements **DB > yaml > defaults**. `config.yaml` carries every spec default. |
| Logging | `sentinel/core/logging.py`: structlog, UTC ISO timestamps, JSON in prod / console in dev, stdlib + uvicorn routed through the same chain, credential-key and `SecretStr` redaction. |
| Storage | `storage/base.py` (declarative base + constraint naming convention), `storage/db.py` (async engine, session factory, non-raising `ping()`). No tables yet. |
| Migrations | Alembic scaffolded, URL read from `DATABASE_URL` only, async `env.py`, empty `0001_baseline` revision. Migrations run at container boot. |
| App | `core/app.py`: FastAPI with **one** route, `/health` (DB ping + scheduler state + last-cycle age), APScheduler with a heartbeat job. `main.py` entrypoint. |
| Ops | `Dockerfile` (slim, non-root), `docker-compose.yml` (app + postgres, both healthchecked, `restart: unless-stopped`, DB bound to loopback), `.env.example`, `Makefile` (`install test lint format typecheck check run up down restart logs ps migrate revision shell clean`). |
| Tests | 47 tests: config layering & spec fidelity, secret redaction, log shape/UTC, `/health` ok + degraded paths, module-tree imports, clock awareness. |

## 2. Demo

```bash
cp .env.example .env          # placeholders are enough for M0 — no external API is called
make up                       # build + start; refuses to run if .env is missing
curl -s localhost:8000/health # → {"status":"ok","checks":{"database":"ok","scheduler":"running"},...}
docker compose exec app alembic current   # → 0001_baseline (head)
make check                    # test + lint + typecheck
```

Verified on this machine (Docker 29.4, Compose v5, Python 3.12.13):

- Both containers reach `healthy`; `/health` → **200**.
- `alembic current` → `0001_baseline (head)`.
- `docker compose stop postgres` → `/health` → **503** `{"status":"degraded","checks":{"database":"error"}}`; restart → back to 200.
- `SENTINEL_ENV=prod` → one JSON log line per event, uvicorn and APScheduler included.
- `make up` without `.env` → explicit failure with the `cp .env.example .env` instruction (no half-configured start).

## 3. Decisions made

1. **Dependencies are milestone-scoped.** M0 installs only what it runs (fastapi, uvicorn, pydantic(+settings), structlog, sqlalchemy, asyncpg, alembic, pyyaml, apscheduler). `ccxt`, `pandas`, `pandas-ta`, `mplfinance`, `anthropic`, `aiogram`, `feedparser` are each >1MB and will be proposed for approval in the milestone that needs them (CLAUDE.md). Approved by the owner before implementation.
2. **Scheduler present, orchestrator absent.** ARCHITECTURE specifies `/health` = "scheduler heartbeat + DB ping + last-cycle age", so APScheduler starts in M0 with a heartbeat job only. `last_cycle_age_seconds` is `null` until the cycle orchestrator lands in M7 — deliberately null rather than a fabricated value.
3. **Empty baseline migration.** `0001_baseline` creates nothing; it exists so the chain has a parent and `alembic upgrade head` is exercised on every boot. The domain schema arrives with M1, as its own migration.
4. **Money-ish config values are `Decimal`.** Percentages that feed sizing (`risk_per_trade_pct`, ladder weights, …) are parsed to `Decimal` via `str`, so no float artefact can reach the risk engine.
5. **Config is strict and frozen.** `extra="forbid"` — a typo in `config.yaml` fails loudly at startup instead of silently falling back to a default. Immutable, so nothing mutates risk settings at runtime by accident.
6. **Redaction by key shape, not by convention.** Log keys matching a credential name (exact or `_token`/`_secret`/`_key`… suffix) are replaced with `***redacted***`, as are `SecretStr` values. Matching is whole-key/suffix on purpose: a substring rule swallowed `tokens_in`/`tokens_out`, which CLAUDE.md requires us to log for every LLM call.
7. **`.env` guard on Docker targets.** `make up|restart|migrate|revision|run` abort with a copy-paste fix when `.env` is missing, rather than booting on half-empty config (owner request during plan review).
8. **`.gitignore` ignores generated charts narrowly** — `/charts_out/` and `sentinel/**/*.png`, not a blanket `*.png`, so M3 chart fixtures and journal images stay committable (owner request).
9. **`owner_timezone` defaults to `UTC`.** PRD §8 leaves the owner's timezone/quiet hours open; UTC is the honest default rather than an invented zone. Internals are UTC regardless — this only affects Telegram rendering later.

## 4. Deviations from spec

**None.** Two judgment calls worth flagging, both inside spec latitude:

- The heartbeat job (decision 2) is not named in any spec; it is the minimum that makes the specified `/health` scheduler field meaningful in M0.
- `storage/base.py` fixes a constraint naming convention now. No tables are defined, but the convention must precede the first migration to keep later migrations reversible.

## 5. Found & fixed during verification

`/health` returned **500** instead of 503 when Postgres was stopped: an unreachable host surfaces as `socket.gaierror`/`OSError` from asyncpg, not as `SQLAlchemyError`, so the original `except SQLAlchemyError` missed it. The unit test used a stub and passed — only the Compose run caught it. `Database.ping()` now degrades on any exception, `/health` guards the probe as well, and two regression tests cover it (raising stub → 503; real `Database` against an unresolvable host → `False`).

## 6. Notes for the next milestone (M1)

- `MarketSnapshot` and friends go in each module's `models.py` (Pydantic v2), per CLAUDE.md.
- Staleness budgets are already in `config.yaml` under `data_quality.max_age_seconds` (from DATA_SOURCES §4) — M1 consumes them rather than redefining them.
- `sentinel/tools/` is scaffolded and empty; the M1 demo command `python -m sentinel.tools.snapshot BTCUSDT` lands there.
- M1 will need `ccxt` (+ `feedparser`, `pandas`): >1MB each, so it needs sign-off before it goes into `pyproject.toml`.
- Local runs outside Docker need `DATABASE_URL` pointed at `localhost` (the `.env.example` comment says so); the tests need no database at all.
