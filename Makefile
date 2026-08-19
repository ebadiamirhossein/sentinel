PY ?= python3.12
VENV := .venv
BIN := $(VENV)/bin

# Every target here is for LOCAL DEVELOPMENT, and layers docker-compose.dev.yml
# on top of the deployment file — that overlay publishes Postgres on 127.0.0.1 so
# `make run`, alembic and the sentinel.tools.* CLIs can reach it from the host
# venv. The SERVER uses plain `docker compose` and never picks the overlay up,
# because 5432 there belongs to another application (docs/DEPLOY.md §3).
COMPOSE ?= docker compose -f docker-compose.yml -f docker-compose.dev.yml

.DEFAULT_GOAL := help
.PHONY: help install test lint format typecheck coverage-risk check-ops check-wheel check-image check-fast check run up down restart logs ps migrate revision shell clean require-env backup verify-backup

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ── Local development ────────────────────────────────────────────────────────

install: $(VENV)/.installed ## Create .venv and install the package with dev extras

$(VENV)/.installed: pyproject.toml
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev]"
	@touch $@

test: install ## Run the test suite
	$(BIN)/pytest

lint: install ## Ruff lint + format check
	$(BIN)/ruff check sentinel tests alembic
	$(BIN)/ruff format --check sentinel tests alembic

format: install ## Apply Ruff formatting and safe fixes
	$(BIN)/ruff format sentinel tests alembic
	$(BIN)/ruff check --fix sentinel tests alembic

typecheck: install ## mypy --strict
	$(BIN)/mypy sentinel tests

coverage-risk: install ## Risk engine: 100% branch coverage or fail (specs/RISK_ENGINE.md)
	$(BIN)/pytest tests/risk --cov=sentinel.risk --cov-branch --cov-fail-under=100

# ── Packaging (M7) ───────────────────────────────────────────────────────────
#
# journal/M6_REPORT.md §12: the Docker image was unbuildable from M5 until M6.2
# and no milestone gate caught it, because tests, ruff and mypy all run from a
# source checkout where prompt files resolve by path whether or not the wheel is
# correct. Only building the artifact exercises packaging. Both targets below are
# in `check` and neither ever skips silently — a skipped packaging check is
# exactly how the break stayed hidden for two milestones.

check-ops: ## Syntax-check the deploy scripts (shellcheck too, if installed)
	@# M8. Same lesson as check-image, one layer out: ops/*.sh run only on the
	@# server, so a typo in them is found at 3am by the person who needed the
	@# backup. `bash -n` needs no extra tooling and catches the whole class;
	@# shellcheck is used when present and never required.
	@for script in ops/*.sh; do bash -n "$$script" || exit 1; done
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck -x ops/*.sh || exit 1; \
		echo "ops scripts: bash -n + shellcheck clean"; \
	else \
		echo "ops scripts: bash -n clean (install shellcheck for the deeper check)"; \
	fi

check-wheel: install ## Build the wheel — catches a broken package in ~2s
	@rm -rf .build-wheel
	$(BIN)/python -m pip wheel --no-deps -q -w .build-wheel .
	@echo "wheel builds cleanly"

check-image: ## Build the image AND import the app inside it
	@docker version >/dev/null 2>&1 || { \
		printf '\033[31mERROR: Docker is not available.\033[0m\n'; \
		printf 'Building the image is part of the milestone gate — see\n'; \
		printf 'journal/M6_REPORT.md §12, where a packaging break hid for two\n'; \
		printf 'milestones because `make check` only ever ran from a source tree.\n'; \
		printf 'Start Docker, or run `make check-fast` if you are mid-edit.\n'; \
		exit 1; \
	}
	docker build -t sentinel:check .
	@# Building is not enough. `pip install` never imports the package, so a
	@# dependency someone forgot to declare — present in .venv, absent from the
	@# image — builds perfectly and dies at boot. So the gate imports the app and
	@# loads a prompt file from the installed wheel, which is the packaging M5's
	@# force-include was trying to guarantee in the first place.
	docker run --rm --entrypoint python sentinel:check -c \
		"import sentinel.main; \
		 from sentinel.analyst.prompts.loader import load_prompt; \
		 assert load_prompt('fable_v1').strip(), 'prompt text missing from the wheel'; \
		 from sentinel.core.config import load_config; cfg = load_config(); \
		 v = cfg.llm.screener_prompt_version; \
		 assert load_prompt(v).strip(), f'configured screener prompt {v} is not in the wheel'; \
		 print('image imports, prompts ship, config loads')"

check-fast: test lint typecheck coverage-risk check-ops ## The gate without the image build
check: check-fast check-wheel check-image ## Everything the milestone gate requires

run: install require-env ## Run the app locally (no Docker)
	$(BIN)/python -m sentinel.main

# ── Docker ───────────────────────────────────────────────────────────────────

require-env: ## Fail fast if .env is missing
	@test -f .env || { \
		printf '\033[31mERROR: .env is missing.\033[0m\n'; \
		printf 'Sentinel refuses to start with half-empty config.\n'; \
		printf 'Create it first:\n\n    cp .env.example .env\n\n'; \
		printf 'then fill in POSTGRES_PASSWORD, DATABASE_URL and your API keys.\n'; \
		exit 1; \
	}

up: require-env ## Build and start app + postgres
	$(COMPOSE) up -d --build

down: ## Stop the stack (keeps the postgres volume)
	$(COMPOSE) down

restart: require-env ## Recreate the app container
	$(COMPOSE) up -d --build app

logs: ## Follow app logs
	$(COMPOSE) logs -f app

ps: ## Show container status and health
	$(COMPOSE) ps

migrate: require-env ## Apply migrations inside the app container
	$(COMPOSE) run --rm app alembic upgrade head

revision: require-env ## Autogenerate a migration: make revision m="add signals table"
	@test -n "$(m)" || { echo 'ERROR: pass a message, e.g. make revision m="add signals table"'; exit 1; }
	$(COMPOSE) run --rm app alembic revision --autogenerate -m "$(m)"

shell: ## Open a shell in the app container
	$(COMPOSE) exec app sh

# ── Ops (docs/DEPLOY.md) ─────────────────────────────────────────────────────
#
# The scripts themselves are host-side and take no make: on the server they are
# run by cron and by hand. These two targets exist so the same commands can be
# rehearsed locally against the development stack.

backup: require-env ## pg_dump the running database into $BACKUP_DIR
	./ops/backup.sh

verify-backup: require-env ## Restore the newest dump into a scratch DB and check it
	./ops/verify-backup.sh

clean: ## Remove caches and the virtualenv
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
