# ADR-0002: PostgreSQL queue with leases and outbox; tenant isolation via row-level security

Date: 2026-10-16. Status: accepted.

Durable jobs are rows in `sync_job` claimed with `FOR UPDATE SKIP LOCKED`, leased with a token and expiry,
retried with capped jittered backoff, dead-lettered after `max_attempts`, replayable and cancellable. Records,
checkpoints and outbox events are committed in one transaction. Temporal is not adopted; if long-running
multi-step workflows (e.g. SharePoint full crawl + ACL reconciliation) prove hard to express as leased jobs, a
follow-up ADR will make that case with evidence from the pilot.

Tenant isolation is enforced in the database: `FORCE ROW LEVEL SECURITY` on every tenant table with a policy on
`current_setting('wayam.tenant_id')`. Application-level `WHERE tenant_id = ...` filters remain as defence in depth.
The test `test_two_tenants_same_source_ids_never_cross` proves a query without a tenant filter returns nothing
for another tenant and nothing when no tenant is set.
