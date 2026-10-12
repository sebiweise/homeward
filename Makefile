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

ARCH ?= x64

spk:
	python tools/build_spk.py --arch $(ARCH)
