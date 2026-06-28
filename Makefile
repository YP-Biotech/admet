.PHONY: setup dev build test test-all test-core test-analyze test-control lint fmt clean

setup:
	uv sync

dev:
	uv run admet analyze

build:
	uv build

test: test-core

test-all: test-core test-analyze test-control

test-core:
	uv run -m unittest discover -s tests

test-analyze:
	uv run -m unittest discover -s tests -p '*analyze*.py'

test-control:
	uv run -m unittest discover -s tests -p '*control*.py'

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
