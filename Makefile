.PHONY: install dev test lint analyze

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

dev:
	.venv/bin/uvicorn app.main:app --app-dir backend --reload --port 8000

test:
	.venv/bin/pytest

lint:
	.venv/bin/ruff check backend tests

analyze:
	.venv/bin/python -m app.cli --data-dir data/raw

