.PHONY: install test lint format spk

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .
	ruff format --check .
	mypy

format:
	ruff check --fix .
	ruff format .

spk:
	@echo "spk build not implemented yet (milestone M5), ARCH=$(ARCH)" && exit 1
