VENV := .venv
BIN := $(VENV)/bin

.PHONY: install up test lint ingest evals

install:  ## create .venv and install the project with dev tools
	python3.12 -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

up:  ## start db and app with docker compose
	docker compose up -d --build

test:  ## pytest
	$(BIN)/pytest

lint:  ## ruff check and format --check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

ingest:  ## run the daily job once
	@echo "ingest: not implemented yet"

evals:  ## run evals on the frozen snapshot
	@echo "evals: not implemented yet"
