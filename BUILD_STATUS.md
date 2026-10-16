# BUILD_STATUS

Last session: 2026-10-16 (session 3). Read this before resuming. Earlier session notes follow the session 3 block.

## Session 3: completed scopes

* **Worker sync engine** (`worker/handlers.py`): `record_sync` drives any RecordSync adapter page by page through
  `RecordStore.commit_page` (cursor from `checkpoint`, reset on cursor-version change; lease heartbeat per page;
  `resume=True` re-enqueues the partition when the page budget is spent; `CursorExpiredError` quarantined as an
  explicit resync). Full crawls record `crawl_seen` marks in the page transaction (migration `0003`); deletions are
  inferred only after a complete crawl and only for `full_crawl_reconcile` sources; abandoned crawl marks are swept.
  `webhook_process` turns inbox rows into envelopes and ignores states older than the stored version (out-of-order).
  Tenant-scoped claims (`JobQueue.claim(tenant_id=...)`) for dedicated worker pools and isolated tests.
* **API**: `POST /connections/{id}/sync` enqueues a `record_sync` job; webhook receipt enqueues `webhook_process`.
* **`aws.s3` adapter** (WCN-119) on boto3 1.43.114: allowlisted bucket/prefixes, ListObjectsV2 cursors,
  ListObjectVersions with delete markers as delete events, HeadObject/GetObject with size cap, STS assume-role and
  ambient auth modes, error classification; knowledge.document mapping `knowledge.s3/1`. Fixture-tested on
  botocore `Stubber` only (no AWS account).
* **Chakram lineage through the engine**: the real `github.repositories` adapter bound to captured fixtures syncs
  repository, commits and PRs into engineering records via worker jobs (`test_chakram_lineage_commits_and_runs_through_the_engine`).

## Session 3: tests and results

```
ruff check / format --check                 -> clean
mypy                                        -> Success: no issues found in 67 source files
validate-manifests / validate-catalog       -> 7 connectors, catalog OK
pytest -q (offline lane)                    -> 109 passed, 3 skipped (live tests without credentials)
```
Acceptance scenarios newly covered: T-12 (expired cursor -> controlled resync), T-13 (replay counts), T-14 (partial
crawl never deletes), T-15 (complete crawl infers deletion; event sources never infer), T-16/T-17 (page failure leaves
checkpoint untouched; replay idempotent), T-19 (cancel continuation), T-26 (out-of-order update/delete), S3 prefix
policy and pagination checks.

## Session 3: unresolved blockers

1. S3 has no live evidence (no AWS credentials/bucket). Needs `WAYAM_LIVE_S3_*` and a live test file (not written yet).
2. Same network blockers as session 2 for docs, Graph, Jira, Vercel; GitHub log storage origin still denied.
3. `webhook_process` relies on `source_timestamp`/`source_version` ordering; providers whose events carry neither
   (push commits) are applied in delivery order only.

## Session 3: next runnable steps

1. `make test`; `WAYAM_LIVE_GITHUB=1 ... make test-live`.
2. Live S3 test file behind `WAYAM_LIVE_S3_BUCKET`/credentials (mirror the GitHub live pattern), then a knowledge
   pipeline job: S3 object -> fetch_document -> files.docx/pptx parse -> knowledge.fragment commit (Bodham ingest).
3. Microsoft Graph transport + SharePoint/OneDrive once graph.microsoft.com is reachable and a tenant exists;
   Jira Cloud and Vercel likewise; SQL Server once an instance is available.
4. OpenTelemetry instrumentation of Store/JobQueue/ProviderHttp.

## Session 2: completed scopes

* **Shared HTTP transport** (`platform/http.py`): policy-bound httpx client (allowlisted origins, https only, no
  credentials in URLs, private/metadata ranges rejected on direct egress; DNS check auto-disabled behind an egress
  proxy), single validated redirect hop that strips the provider token and API media headers for pre-signed storage
  URLs, size cap, `Retry-After`/`X-RateLimit` classification, `Link` pagination helper, redaction of error details.
* **GitHub provider core + two surfaces**: `github.repositories` (ConnectionCheck incl. write-capable-credential
  detection, ResourceDiscovery, RecordSync for repository/commit/pull_request/issue with overlap watermarks and
  Link-URL resume cursors, WebhookReceiver with X-Hub-Signature-256) and `github.actions` (ConnectionCheck,
  RecordSync for workflow_run/workflow_job/artifact, DocumentFetch for redacted run logs; no dispatch/rerun).
  Engineering mapping `engineering.github/1`, new `WorkflowJob` domain record, `AdapterContext` binding.
* **Real provider fixtures** captured from api.github.com for mrigankad/Chakram (`fixtures/github/`, emails
  redacted, headers trimmed); PR/issue fixtures are SYNTHETIC and labelled inside the files.
* **Bodham knowledge path**: migration `0002_knowledge_access.sql` (source principals, per-document ACL state,
  principal mapping), `KnowledgeAccessService` (nested groups, 15-minute ACL evidence window, fail-closed on unknown
  identity/stale ACL/deleted document/revoked connection), DOCX/PPTX import to `knowledge.fragment` with document ids.
* **API**: `POST /webhooks/{connection_id}` (tenant from route header, raw-byte signature verification, durable
  inbox, dedup). **SDKs**: `scripts/generate_sdks.py` produces `packages/sdk-python` (httpx client, one method per
  operation) and `packages/sdk-typescript` (openapi-typescript schema + fetch client); CI checks the Python SDK is
  fresh. **CI**: scheduled live canary job wired to `WAYAM_LIVE_GITHUB_TOKEN`.

## Session 2: tests and results

```
ruff check / ruff format --check            -> All checks passed / 85 files already formatted
mypy                                        -> Success: no issues found in 62 source files
validate-manifests / validate-catalog       -> 6 connectors, catalog OK
pytest -q (offline lane)                    -> 100 passed, 3 skipped (the live tests, BLOCKED without WAYAM_LIVE_GITHUB=1)
WAYAM_LIVE_GITHUB=1 WAYAM_LIVE_GITHUB_AUTH_MODE=ambient pytest -m live
                                            -> 2 passed (repositories, actions runs/jobs/artifacts), 1 skipped:
                                               BLOCKED log download, storage origin denied by egress policy (CONNECT 403)
```
Live environment: api.github.com, `X-GitHub-Api-Version-Selected: 2022-11-28`, credentials injected by the session
egress gateway (write-capable; adapters are read-only by construction and the check reports it).
Acceptance scenarios newly covered: T-09 (pagination cycle), T-10/T-11 (equal timestamps, overlap), T-20 (429/Retry-After),
T-21 (5xx/timeouts), T-24 (bad signature), T-25 (duplicate delivery), T-27, T-28, T-29, T-30, T-31, T-44 (no
dispatch/rerun; transport refuses POST), T-45 (repository allowlist), T-47 (redirect origin policy, token stripped),
Bodham journey (cited fragments behind ACLs). Still open: OAuth group T-04..T-08 (no OAuth provider yet), T-26 ordering
across webhook + poll, T-39, Chakram lineage journey (needs Vercel), SharePoint/OneDrive/S3/Jira/Vercel/SQL Server.

## Session 2: unresolved blockers

1. Vendor documentation hosts still blocked (403) from the session; GitHub contract pages mark every item
   *observed* or *knowledge* (`docs/provider-contracts/github-*.md`). Confirm against current docs when reachable.
2. GitHub log storage origin denied by the egress policy: `fetch_document` is fixture-tested only.
3. api.vercel.com, graph.microsoft.com and *.atlassian.net are unreachable (connection refused by policy); S3 host
   reachable but no AWS credentials. Those surfaces remain `candidate`.
4. Token-mode GitHub auth and private repositories not live-tested (needs `WAYAM_LIVE_GITHUB_TOKEN` in CI secrets).
5. TypeScript SDK `schema.d.ts` is generated via `npx openapi-typescript@7.6.1`; it ran in this session but CI only
   checks the Python SDK freshness (npm availability in CI not assumed).

## Session 2: next runnable steps

1. `make test` then `WAYAM_LIVE_GITHUB=1 WAYAM_LIVE_GITHUB_TOKEN=<fine-grained read-only PAT> make test-live`.
2. Worker handler `record_sync` that drives `RecordSync` adapters page by page through `RecordStore.commit_page`
   (cursor from `checkpoint`, lease heartbeat per page, partial failures to quarantine) and a webhook processor
   that turns inbox rows into envelopes via `envelopes_from_delivery` with out-of-order handling (T-26).
3. Chakram lineage journey once Vercel is reachable: commit -> PR -> workflow run -> deployment.
4. SQL Server, S3, Graph (SharePoint/OneDrive), Jira, Vercel as before (see session 1 steps), in that order of
   credential availability.

---

# Session 1 (2026-10-16)

## Completed scopes

* **Repository and toolchain**: `mrigankad/Connectors-` was empty; scaffolded as one Python distribution
  (ADR-0001), pinned deps + `uv.lock`, ruff/mypy strict, pytest markers (`integration`, `live`, `e2e`), CI offline
  lane with a PostgreSQL service container, Dependabot, Docker Compose, Makefile, dev DB script.
* **Contracts** (`src/wayam_connectors/contracts`): 11 capability Protocols incl. `FileParser`, typed error
  classes driving retry/block/quarantine decisions, `ConnectorManifest` (id, catalog id, family, surface, edition,
  versions, schemas, operations/scopes, capability flags, cursor version, rate policy, raw-data support, deletion
  semantics, ACL coverage, owner, limitations, verification level), `SourceEnvelope` with namespaced extensions,
  domain schemas for finance (Decimal `Money`), knowledge (anchors/completeness/principals), manufacturing and
  engineering.
* **Platform state** (`src/wayam_connectors/platform`): migration `0001_core.sql` with 15 tables and RLS on all
  tenant tables; `Store.tenant_tx`/`bypass_tx`; API keys (sha256 at rest); capability grants; AES-GCM credential
  refs bound to tenant+connection with rotation/revocation; lease-based job queue with jittered backoff,
  Retry-After, dead-letter, replay, cancel, SKIP LOCKED claims; atomic `commit_page` (records + quarantine +
  checkpoint + outbox); webhook inbox (verify raw bytes → durable insert → dedup); encrypted raw evidence store;
  registry with adapter-conformance and catalog cross-checks; redaction, SSRF origin policy, OOXML container checks.
* **Connectors**: `files.csv` (explicit mapping, locale, types, duplicate keys, provenance, hash);
  `postgresql.bounded_query` (read-only role verification, allowlisted discovery, guarded bounded queries, RLS user
  context, statement timeout); `files.docx`; `files.pptx` (image-only slides marked incomplete).
* **Mapping**: `finance.bank_transaction.csv/1` with 4 sign conventions, derived ids, reversals, control-total
  reconciliation that fails the import without writing.
* **Pipeline + API + worker**: `FileImportService` (grant check → hash dedup → parse → map → reconcile → atomic
  commit); FastAPI control API (bearer API key → tenant; connections, grants, check, discover, query, imports,
  records, quarantine, jobs); worker loop with `connection_check` handler; CLI.

## Tests and results (this session)

```
.venv/bin/python -m ruff check src tests scripts      -> All checks passed
.venv/bin/python -m mypy                             -> Success: no issues found in 52 source files
.venv/bin/python -m wayam_connectors.cli validate-manifests && validate-catalog -> OK
.venv/bin/python -m pytest -q                        -> 86 passed (unit 53, integration 29, e2e 4), ~6 s
```
Environment: Python 3.13.16, PostgreSQL 16.15 (local cluster, role `wayam` NOSUPERUSER CREATEDB CREATEROLE),
psycopg 3.3.6, python-docx 1.2.0, python-pptx 1.0.2, fastapi 0.143.3. Live lane: nothing run (no credentials).
Acceptance scenarios covered: T-01, T-02, T-03, T-09 (partition/dup guard), T-13, T-16, T-17, T-18, T-19, T-20,
T-24, T-25, T-32, T-33, T-34, T-35, T-36, T-37, T-38, T-40, T-41, T-42, T-43, T-46 (log/result redaction),
T-47 (origin policy only), T-48 (SAMYAK import journey). Not yet: OAuth group (T-04..T-08), webhook ordering T-26,
ACL group T-27..T-31, T-39, T-44, T-45, Bodham/Chakram journeys.

## Unresolved blockers

1. **Vendor documentation hosts are blocked by the session egress policy (403)**: learn.microsoft.com,
   docs.github.com and others. PyPI works. Recorded in ADR-0004; the eight remaining P0 provider contracts are
   written as verification checklists, not as endpoint claims.
2. **No provider credentials or sandboxes** (S3, M365, GitHub, Jira, Vercel, SQL Server). See
   `docs/credential-matrix.md`. Those surfaces stay `candidate` with no code.
3. **Azure Key Vault backend** not implemented (needs azure SDKs + environment); production refuses the local backend.
4. **Office fixtures are library-generated**; Word/PowerPoint-authored samples are needed before `sandbox_verified`.
5. **Docker daemon unavailable in the session**: compose file written, not executed here.
6. **SDK generation**: `packages/contracts/openapi.json` is exported and checked in CI; TypeScript/Python client
   generation not yet wired (needs a generator choice: openapi-typescript / openapi-python-client).

## Next runnable steps (in order)

1. `make test` (needs local PostgreSQL; `sudo -u postgres scripts/dev_db.sh` once).
2. SQL Server slice: choose driver (pyodbc + ODBC 18 likely), add a SQL Server instance to the compose/CI lane,
   implement `sqlserver.bounded_query` reusing `sql_guard` patterns but with T-SQL specifics (no READ ONLY
   transaction mode; use a read-only login + `SET TRANSACTION ISOLATION LEVEL`, driver query timeout, `TOP`,
   `SESSION_CONTEXT` for per-user policy). Do not reuse the PostgreSQL adapter.
3. Knowledge fragment import for DOCX/PPTX through `FileImportService` (`domain_mapping="knowledge.fragment"`
   already routed) plus a Bodham E2E test with ACL stub rows in `source_acl`.
4. S3 adapter once an AWS test bucket exists; until then write the boto3 contract tests behind `live` markers.
5. Shared Microsoft Graph transport (httpx + OriginPolicy + Retry-After + token refresh serialization) before
   SharePoint/OneDrive; GitHub core before repositories/Actions; then Jira, Vercel.
6. OpenTelemetry instrumentation of `Store`, `JobQueue` and adapters (deps already pinned).
7. SDK generation from `packages/contracts/openapi.json`.

## Changed files (session 1)

Everything under this repository is new in this session. Chakram (`mrigankad/Chakram`) was inspected
(`backend/app/services/{github,jira,vercel,connector_settings}_service.py`) for its integration contracts but not
modified.
