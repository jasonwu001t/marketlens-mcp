# marketlens-mcp development tasks. Requires uv (https://docs.astral.sh/uv/).
PY ?= .venv/bin/python
RUN = $(PY) -m
# Generated files come from the built-ins at their defaults, never from your own config.
DEFAULTS_ENV = MARKETLENS_CONFIG= XDG_CONFIG_HOME=$$(mktemp -d)

.PHONY: setup venv install test test-all lint fmt readme schema check build sync-alpaca-specs clean

setup: venv install

venv:
	uv venv .venv

install:
	uv pip install --python $(PY) -e ".[dev]"

test:
	PYTHONWARNDEFAULTENCODING=1 $(RUN) pytest

test-all:
	PYTHONWARNDEFAULTENCODING=1 MARKETLENS_CHECK_GENERATED=1 $(RUN) pytest

lint:
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

fmt:
	$(RUN) ruff check --fix src tests
	$(RUN) ruff format src tests

readme:
	$(DEFAULTS_ENV) $(RUN) marketlens_mcp readme

schema:
	rm -rf schema
	$(DEFAULTS_ENV) $(RUN) marketlens_mcp schema --out schema

check: lint test-all

build:
	uv build

sync-alpaca-specs:
	sh scripts/sync-alpaca-specs.sh

clean:
	rm -rf dist build .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
