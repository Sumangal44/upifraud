# ============================================================================
# UPI Fraud Detection — task runner
# ============================================================================
COMPOSE ?= docker compose
PY      ?= python3

.DEFAULT_GOAL := help

.PHONY: help up down clean ps logs seed loadgen train venv \
        test test-rust test-py test-ts fmt build-rust init

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- platform ---
up: ## Build images and start the full platform
	$(COMPOSE) up -d --build

down: ## Stop the platform
	$(COMPOSE) down

clean: ## Stop and delete volumes (data wipe)
	$(COMPOSE) down -v

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Tail logs of application services
	$(COMPOSE) logs -f gateway feature-worker stream-api inference xai

seed: ## Seed Memgraph + Dragonfly with demo entities and a money-mule ring
	$(COMPOSE) run --rm seed

loadgen: ## Generate synthetic traffic (foreground; Ctrl-C to stop)
	$(COMPOSE) run --rm loadgen

# --------------------------------------------------------------------- dev ---
# Python targets prefer the project venv (created by `make venv` or manually).
VENV_PY := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo $(PY))

venv: ## Create the Python venv and install all Python deps
	$(PY) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r ml/training/requirements.txt -r ml/inference/requirements.txt \
		-r ml/xai/requirements.txt -r tools/requirements.txt

init: ## Install local dev dependencies (Rust/Node already assumed installed)
	cargo fetch
	$(VENV_PY) -m pip install -r ml/training/requirements.txt -r ml/inference/requirements.txt \
		-r ml/xai/requirements.txt -r tools/requirements.txt
	npm --prefix apps/portal install

build-rust: ## Release-build all Rust services
	cargo build --workspace --release

train: ## Train GNN + XGBoost models into ml/artifacts/ (CPU fallback)
	PYTHONPATH=. $(VENV_PY) -m ml.training.train

fmt: ## Format Rust code
	cargo fmt --all

# ------------------------------------------------------------------ tests ---
test: test-rust test-py test-ts ## Run all test suites

test-rust: ## Rust unit tests (includes upifraud-core policy tests)
	cargo test --workspace

test-py: ## Python unit tests (heuristic scorer, schemas, tools)
	$(VENV_PY) -m pytest ml tools -q

test-ts: ## Portal lint + type-check
	npm --prefix apps/portal run lint
