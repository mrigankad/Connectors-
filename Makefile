PY := .venv/bin/python

.PHONY: setup lint typecheck validate test test-unit test-integration test-live migrate openapi demo

setup:            ## create venv, install pinned deps
	uv venv --python 3.12 .venv && uv pip install --python $(PY) -e ".[dev]"
lint:
	$(PY) -m ruff check src tests scripts && $(PY) -m ruff format --check src tests scripts
typecheck:
	$(PY) -m mypy
validate:         ## manifests + catalog registry
	$(PY) -m wayam_connectors.cli validate-manifests && $(PY) -m wayam_connectors.cli validate-catalog
test:             ## everything except live
	$(PY) -m pytest -q -m "not live"
test-unit:
	$(PY) -m pytest -q tests/unit tests/contract
test-integration:
	$(PY) -m pytest -q tests/integration tests/e2e
test-live:        ## opt-in; blocked (not faked) without credentials
	WAYAM_LIVE=1 $(PY) -m pytest -q -m live
migrate:
	$(PY) -m wayam_connectors.cli migrate
openapi:
	$(PY) -m wayam_connectors.cli export-openapi packages/contracts/openapi.json
demo:             ## migrate, create a tenant, print an admin key and start the API
	$(PY) -m wayam_connectors.cli migrate && $(PY) -m wayam_connectors.cli sync-definitions && \
	T=$$($(PY) -m wayam_connectors.cli create-tenant demo) && echo "tenant $$T" && \
	echo "admin key: $$($(PY) -m wayam_connectors.cli issue-key $$T platform --admin)" && \
	$(PY) -m wayam_connectors.cli serve
