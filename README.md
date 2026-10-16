# Wayam shared connector platform

Multi-tenant integration platform reused by SAMYAK, Bodham, Chakram, Vedha and Anvaya: typed connector
contracts, provider adapters, a durable sync runtime on PostgreSQL, and a control API. Research inputs live
unmodified in `catalog/research/`; implementation status lives in `catalog/registry.v1.json` and
`docs/verification-matrix.csv`. Read `BUILD_STATUS.md` first when resuming work.

## Quick start (fresh clone)

```bash
# PostgreSQL 16 running locally; as a superuser:
sudo -u postgres scripts/dev_db.sh            # creates role wayam and the dev/test databases
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
cp .env.example .env                           # set WAYAM_LOCAL_SECRET_KEY (base64 of 32 random bytes)
make lint typecheck validate test              # ruff, mypy, manifest/catalog validation, all non-live tests
make demo                                      # migrate, create a tenant, print an admin API key, serve on :8090
```

Docker Compose for API + worker + database: `cp infra/.env.example infra/.env && docker compose -f infra/docker-compose.yml up`.

## What is implemented

| Connector | Capabilities | Level |
|---|---|---|
| `files.csv` (WCN-283) | FileParser | local_integration_verified |
| `postgresql.bounded_query` (WCN-140) | ConnectionCheck, ResourceDiscovery, BoundedQuery | local_integration_verified |
| `files.docx` (WCN-285) | FileParser | fixture_tested |
| `files.pptx` (WCN-286) | FileParser | fixture_tested |
| `github.repositories` (WCN-097) | ConnectionCheck, ResourceDiscovery, RecordSync, WebhookReceiver | sandbox_verified (live read on mrigankad/Chakram) |
| `github.actions` (WCN-099) | ConnectionCheck, RecordSync, DocumentFetch | sandbox_verified (logs fixture-only) |
| `aws.s3` (WCN-119) | ConnectionCheck, ResourceDiscovery, RecordSync, DocumentFetch | fixture_tested (botocore stubs) |

Everything else in the 294-entry catalog is `candidate` and has no code. See `docs/architecture.md`,
`docs/decisions/`, `docs/provider-contracts/` and `docs/credential-matrix.md`.

## Commands

| Command | Purpose |
|---|---|
| `wayam-connectors validate-manifests` | load every manifest, check adapter/capability conformance |
| `wayam-connectors validate-catalog` | cross-check manifests, `catalog/registry.v1.json` and the research catalog |
| `wayam-connectors migrate` / `sync-definitions` | apply SQL migrations / upsert connector definitions |
| `wayam-connectors create-tenant NAME`, `issue-key TENANT PRODUCT [--admin]` | tenancy bootstrap |
| `wayam-connectors export-openapi` | write `packages/contracts/openapi.json` (SDK generation input) |
| `python scripts/generate_sdks.py [--python-only]` | regenerate `packages/sdk-python` and `packages/sdk-typescript` from the OpenAPI contract |
| `WAYAM_LIVE_GITHUB=1 WAYAM_LIVE_GITHUB_TOKEN=... make test-live` | live GitHub verification against `WAYAM_LIVE_GITHUB_REPO` (default mrigankad/Chakram) |
| `wayam-connectors serve` / `worker` | run the control API / the job worker (`record_sync`, `document_ingest`, `webhook_process`, `connection_check`; ticks schedules every 30 s) |
| `WAYAM_LIVE_S3=1 WAYAM_LIVE_S3_BUCKET=... make test-live` | live S3 verification (see `tests/live/test_s3_live.py` for the variables) |
| `make test-live` | opt-in live tests; blocked (not faked) without credentials |
