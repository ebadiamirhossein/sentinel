PY ?= python3.12
VENV := .venv
BIN := $(VENV)/bin
COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help install test lint format typecheck coverage-risk check run up down restart logs ps migrate revision shell clean require-env

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

check: test lint typecheck coverage-risk ## Everything the milestone gate requires

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

clean: ## Remove caches and the virtualenv
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
