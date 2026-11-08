.PHONY: install dev test lint format validate openapi check

install:            ## create .venv and install dev dependencies
	python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

dev:                ## full local stack (API + MongoDB + Mailpit)
	docker compose up --build

test:
	python -m pytest

lint:
	ruff check . && ruff format --check .

format:
	ruff format . && ruff check . --fix

validate:           ## validate config/*.yaml with plain-language errors
	python -m scripts.validate_config

openapi:            ## regenerate openapi.json (the frontend generates its client from it)
	python -m scripts.export_openapi

check: lint test validate   ## what CI runs
	python -m scripts.export_openapi --check
