.PHONY: setup dev describe build test test-all test-core test-analyze test-control test-integration lint fmt clean docs-dev docs-build systemd
.DEFAULT_GOAL := setup

ANALYZE_TESTS := $(wildcard tests/test_analyze_*.py tests/test_cellpose_helpers.py tests/test_opencv_engine.py)
CONTROL_TESTS := $(wildcard tests/test_control_*.py tests/test_camera_backend.py tests/test_camera_bench.py tests/test_fluidics.py tests/test_observation.py tests/test_recording_limits.py)
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

# admet analyze as a system-wide systemd service on a Linux server, running as the user who runs this:
# `make systemd`, or with other values, `make systemd ADMET_DATA=/srv/nas/ADMET/data ANALYZE_PORT=9000`.
# The install steps use sudo.
ADMET_HOME ?= $(HOME)/admet
ADMET_DATA ?=
ANALYZE_HOST ?= 0.0.0.0
ANALYZE_PORT ?= 8080
SERVICE_USER ?= $(shell id -un)
SERVICE_GROUP ?= $(shell id -gn)
UNIT := /etc/systemd/system/admet-analyze.service

systemd:
	@command -v systemctl >/dev/null || { echo "systemd is not here: make systemd is for a Linux server"; exit 1; }
	uv sync --locked --extra analyze
	mkdir -p "$(ADMET_HOME)"
	sed -e 's|@USER@|$(SERVICE_USER)|g' -e 's|@GROUP@|$(SERVICE_GROUP)|g' -e 's|@ADMET_HOME@|$(ADMET_HOME)|g' \
	    -e 's|@CHECKOUT@|$(CURDIR)|g' -e 's|@HOST@|$(ANALYZE_HOST)|g' -e 's|@PORT@|$(ANALYZE_PORT)|g' \
	    -e 's|@DATA_ARG@|$(if $(ADMET_DATA), --data $(ADMET_DATA),)|g' \
	    deploy/systemd/admet-analyze.service | sudo tee "$(UNIT)" >/dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable admet-analyze
	sudo systemctl restart admet-analyze
	@echo "admet analyze runs on port $(ANALYZE_PORT) as $(SERVICE_USER); status: systemctl status admet-analyze; log: journalctl -u admet-analyze -f"
