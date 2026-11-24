.PHONY: setup dev describe build test test-all test-core test-analyze test-control test-integration lint fmt clean docs-dev docs-build systemd
.DEFAULT_GOAL := setup

ANALYZE_TESTS := $(wildcard tests/test_analyze_*.py tests/test_cellpose_helpers.py tests/test_opencv_engine.py)
CONTROL_TESTS := $(wildcard tests/test_control_*.py tests/test_camera_backend.py tests/test_fluidics.py tests/test_observation.py tests/test_recording_limits.py)
INTEGRATION_TESTS := tests/test_core_service.py tests/test_json_protocol.py tests/test_operations.py tests/test_observe_operation.py tests/test_protocol_planning.py tests/test_flow_scout.py tests/test_qt_backend.py
CORE_TESTS := $(filter-out $(ANALYZE_TESTS) $(CONTROL_TESTS) $(INTEGRATION_TESTS),$(wildcard tests/test_*.py))
modules = $(patsubst tests/%.py,tests.%,$(1))

setup:
	uv sync --all-extras

dev:
	uv run --extra control admet control

describe:
	uv run admet describe acquisition

build:
	uv build

test: test-core test-control

test-all: test test-integration test-analyze

test-core:
	uv run python tests/run.py $(call modules,$(CORE_TESTS))

test-control:
	uv run python tests/run.py $(call modules,$(CONTROL_TESTS))

test-integration:
	uv run python tests/run.py $(call modules,$(INTEGRATION_TESTS))

test-analyze:
	uv run python tests/run.py $(call modules,$(ANALYZE_TESTS))

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

docs-dev:
	cd docs && bun install && bun run dev

docs-build:
	cd docs && bun install && bun run build

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info docs/.vitepress/dist docs/.vitepress/cache

# admet analyze as a systemd user service on a Linux server: `make systemd`, or with other values,
# `make systemd ADMET_HOME=/srv/admet ADMET_DATA=/srv/nas/ADMET/data ANALYZE_PORT=9000`.
ADMET_HOME ?= $(HOME)/admet
ADMET_DATA ?=
ANALYZE_HOST ?= 0.0.0.0
ANALYZE_PORT ?= 8080
UNIT_DIR := $(HOME)/.config/systemd/user

systemd:
	@command -v systemctl >/dev/null || { echo "systemd is not here: make systemd is for a Linux server"; exit 1; }
	uv sync --locked --extra analyze
	mkdir -p "$(UNIT_DIR)" "$(ADMET_HOME)"
	sed -e 's|@ADMET_HOME@|$(ADMET_HOME)|g' -e 's|@CHECKOUT@|$(CURDIR)|g' -e 's|@HOST@|$(ANALYZE_HOST)|g' \
	    -e 's|@PORT@|$(ANALYZE_PORT)|g' -e 's|@DATA_ARG@|$(if $(ADMET_DATA), --data $(ADMET_DATA),)|g' \
	    deploy/systemd/admet-analyze.service > "$(UNIT_DIR)/admet-analyze.service"
	loginctl enable-linger "$$USER" || echo "could not enable lingering: the service stops when you log out; ask an admin to run 'loginctl enable-linger $$USER'"
	systemctl --user daemon-reload
	systemctl --user enable admet-analyze
	systemctl --user restart admet-analyze
	@echo "admet analyze runs on port $(ANALYZE_PORT); status: systemctl --user status admet-analyze; log: journalctl --user -u admet-analyze -f"
