# CLAUDE.md — Standing Instructions for Claude Code

You are building **Sentinel**, a 24/7 crypto market-research and signal system. It analyzes; a human trades. Read `docs/PRD.md` and `docs/ARCHITECTURE.md` before any work. The spec for the module you're touching (in `docs/specs/`) is the source of truth — if code and spec disagree, the spec wins; if the spec seems wrong, STOP and ask, don't improvise.

## Workflow rules (spec-driven)
1. Work milestone by milestone per `docs/MILESTONES.md`. Never start milestone N+1 with failing tests in N.
2. Before coding a milestone: restate your implementation plan in ≤ 15 bullet points and list files you'll create/modify. Then implement.
3. Every module ships with tests in the same session. `make test lint typecheck` must be green before you declare a milestone done.
4. Risk engine (`sentinel/risk/`) is TDD: write the tests from `docs/specs/RISK_ENGINE.md` §8 FIRST.
5. When done with a milestone, write a short `journal/M{N}_REPORT.md`: what was built, decisions made, deviations from spec (should be none without approval), how to demo.

## Hard constraints — never violate
- **NO trade execution code, ever.** No exchange API keys with trade/withdraw permission, no order-placement functions, not even "for later". This is v1's most important boundary.
- **No LLM in deterministic modules** (`features/`, `risk/`, `tracker/`, `stats/`). Pure functions, Decimal for money.
- **The LLM never sizes, never sets leverage, never approves.** It outputs analysis; the risk gate outputs numbers.
- **Never invent data paths:** if a data source is unavailable, degrade explicitly (see specs/DATA_SOURCES.md §4); never fabricate placeholder market values outside test fixtures.
- Secrets only via `.env` / environment. Never hardcode, never log them.
- All timestamps UTC internally. All money math `Decimal`. Full type hints, `mypy --strict` clean.

## Code standards
- Python 3.12, async-first. Ruff for lint/format. Pydantic v2 models for all cross-module contracts (defined once in each module's `models.py`).
- Small modules, dependency direction: `bot/tracker/analyst/... → storage`, never sideways imports between pipeline stages; the orchestrator in `core/` wires stages together.
- External calls: timeout + max-2 retries + structured error; recorded-cassette tests, never live-API tests in the suite.
- Log every LLM call: model, prompt_version, tokens in/out, cost estimate, duration.

## Prompts
- Prompt text lives ONLY in `sentinel/analyst/prompts/` files. Never inline prompt edits. New behavior = new version file + entry in `journal/PROMPT_LOG.md`.
- Filenames are **per provider**: `fable_v1.md`, `screener_v1.md`, later `sol_v1.md` (specs/ENSEMBLE.md §2, which supersedes the earlier bare `vN.md` here — settled with the owner 2026-08-18 so M10's second provider needs no rename and no rewrite of stored `prompt_version` values).

## Milestone gate
`make check` = tests + ruff + mypy + 100% risk branch coverage + **`check-ops`, `check-wheel`
and `check-image`**. The last two build the wheel and the Docker image and then *import the app
inside the image* — journal/M6_REPORT.md §12 found the image unbuildable for two milestones
because everything else runs from a source checkout, and journal/M7_REPORT.md §6 found that
building alone still misses an undeclared dependency. `check-ops` (M8) is the same lesson one
layer out: `ops/*.sh` run only on the server, so a typo in them is found at 3am by the person
who needed the backup — it runs `bash -n` on every script, and shellcheck too when installed.
None of the three ever skips silently. `make check-fast` is the old gate for mid-edit runs.

## When unsure
Ask. A clarifying question costs a minute; a wrong assumption in a trading system costs money. Specifically ask before: changing any risk default, adding a dependency > 1MB, altering a Pydantic contract, or touching the DB schema outside a migration.
