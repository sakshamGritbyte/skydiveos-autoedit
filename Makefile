.PHONY: install test lint typecheck format clean

install:
	uv sync

test:
	uv run pytest tests/ -q || [ $$? -eq 5 ]

lint:
	uv run ruff check .

typecheck:
	uv run mypy ingest metadata analysis edl render api

format:
	uv run ruff format .

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
