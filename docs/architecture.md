# Architecture

One multi-tenant connector platform reused by SAMYAK, Bodham, Chakram, Vedha, Anvaya (and Sankalp).
Provider authentication and transport are shared; domain mappings and product permissions are separate.

```
products / SDKs ──► control API (FastAPI)  ──► registry (manifests, capability conformance)
                         │                       │
                         ▼                       ▼
                 PostgreSQL state (RLS)   adapters: connectors/<family>/<surface>
                 tenant, api_key, connection, credential_ref, capability_grant,
                 sync_job, checkpoint, normalized_record, quarantine_record,
                 raw_object_ref, source_acl, webhook_inbox, outbox, action_request, audit_event
                         ▲
                 worker (lease-based job loop)
```

## Packages (`src/wayam_connectors`)

| Package | Responsibility |
|---|---|
| `contracts/` | Pure types: capability Protocols, typed errors, `ConnectorManifest`, `SourceEnvelope`, domain schemas (finance, knowledge, manufacturing, engineering). No I/O. |
| `platform/` | Runtime: settings, `Store` (psycopg 3 + RLS transactions), migrations, tenancy (API keys, grants), credentials (AES-GCM behind a `SecretBackend`), job queue, record commit boundary, webhook inbox, raw evidence store, registry, file import pipeline, security helpers (redaction, SSRF policy, file limits). |
| `connectors/<family>/<surface>/` | One adapter per provider surface: `manifest.json` + `adapter.py` exporting `ADAPTER`. An adapter implements exactly the capabilities its manifest declares (checked at load). |
| `mappings/<domain>/` | Source-to-domain mappings with their own versions (e.g. `finance.bank_transaction.csv/1`). |
| `api/` | Control API. Tenant identity comes from the bearer API key, never from a body field. |
| `worker/` | Claims jobs under a lease, runs a typed handler with a `JobGrant`, acks/fails through the queue. |

## Tenancy and isolation

* Every tenant table has `tenant_id` and a row-level-security policy comparing it to the transaction GUC
  `wayam.tenant_id` (`FORCE ROW LEVEL SECURITY`, so the owning role is subject to it). A session that sets no
  tenant sees nothing. The application role is `NOSUPERUSER`.
* Cross-tenant runtime work (queue claim, auth lookup, admin) sets `wayam.bypass` from a closed list of reasons
  inside `Store.bypass_tx`; request handlers never call it.
* Unique keys include tenant and connection: records `(tenant, connection, object type, object id, version key)`,
  checkpoints `(tenant, connection, resource type, resource id)`, raw objects `(tenant, connection, sha256)`,
  webhooks `(tenant, connection, delivery id)`.
* Credentials are AES-256-GCM ciphertext whose associated data is `tenant|connection`; a row copied elsewhere
  cannot be decrypted. Rotation writes a new version and revokes the old; revocation blocks the connection.
* Products hold `capability_grant`s per connection. A missing grant, a guessed connection id and another tenant's
  id all produce the same error.

## Sync correctness

* `RecordStore.commit_page` writes records, quarantine rows, the checkpoint and the outbox event in one
  transaction. A crash before commit loses nothing durable; a replay after commit is counted as `replayed`, never
  stored twice (at-least-once + idempotent storage).
* Jobs: `FOR UPDATE SKIP LOCKED` claim, lease token, heartbeat, capped retries with jittered exponential backoff,
  `Retry-After` honoured, `AuthError`/`ScopeError` block the connection without retry, `dead_letter` after
  `max_attempts`, explicit `replay` and `cancel`. One active job per `(connection, job type, resource)` partition.
* Webhooks: signature verified over the raw bytes before anything is stored; durable insert; duplicates
  acknowledged but not stored twice; headers redacted at rest.

## Sync engine (worker)

* `record_sync` job: cursor from `checkpoint` (reset when the adapter's cursor version changed), one atomic
  `commit_page` per source page, lease heartbeat between pages, `resume=True` + re-enqueue when the page budget
  runs out, `CursorExpiredError` recorded in quarantine and surfaced as an explicit resync.
* Full crawls mark seen objects in `crawl_seen` inside the page transaction; deletions are inferred only when the
  crawl completes and only for `deletion_semantics=full_crawl_reconcile`; abandoned crawls are swept after 7 days
  and never drive deletes.
* `webhook_process` job: inbox rows -> `envelopes_from_delivery` -> drop states older than the stored version of
  the same object (out-of-order deliveries) -> commit per delivery -> mark processed/rejected.

## Security

* Untrusted bytes go through `platform/security/files.py` (size, member count, traversal, compression ratio,
  macros/ActiveX, `<!DOCTYPE`/`<!ENTITY`) before any parser touches them.
* Outbound calls are bound by `OriginPolicy` (allowlisted origins, https only, no credentials in URLs, private and
  metadata ranges rejected unless the connection is explicitly a customer-gateway connection).
* Logs, job results and stored errors pass through `redact()`.
* Mutations need `ReviewedAction`, a grant and an `action_request` row; no connector implements it yet.

## Status ladder

`candidate → contract_ready → fixture_tested → local_integration_verified | sandbox_verified → pilot_verified → supported`,
plus `blocked`, `degraded`, `retired`. The manifest carries the level, `catalog/registry.v1.json` mirrors it and
`docs/verification-matrix.csv` holds the evidence. `validate-catalog` fails if they disagree.
