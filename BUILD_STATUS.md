# BUILD_STATUS

Last session: 2026-10-16 (session 1). Read this before resuming.

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
