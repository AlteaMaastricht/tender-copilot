VENV := .venv
BIN := $(VENV)/bin

.PHONY: install up test lint ingest evals

install:  ## create .venv from uv.lock (needs uv)
	uv sync --locked --extra dev

up:  ## start db and app with docker compose
	docker compose up -d --build

test:  ## pytest
	$(BIN)/pytest

lint:  ## ruff check and format --check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

ingest:  ## run the daily job once
	@echo "ingest: not implemented yet"

# Evals (docs/EVALS.md). Without MAX_COST this is a dry run: it counts calls and estimates
# the cost. Live:  make evals MAX_COST=5   Another model:  make evals MAX_COST=5 STAGE2=...
# Other options:   make evals ARGS="--suites screening --limit 10"
STAGE1 ?= claude-haiku-5-5
STAGE2 ?= claude-sonnet-5-5

evals:  ## run evals on the frozen snapshot
	$(BIN)/python -m evals.run --stage1-model $(STAGE1) --stage2-model $(STAGE2) \
		$(if $(MAX_COST),--max-cost-usd $(MAX_COST),--dry-run) $(ARGS)
