"""Built-in job handlers.

record_sync: drive a RecordSync adapter page by page. Each page is one atomic commit (records, quarantine,
checkpoint, outbox, crawl-seen marks); the lease is renewed after every page; a lost lease or cancel stops
the loop without acknowledging. When the page budget runs out before the source reports completion, the job
returns `resume=True` and the worker re-enqueues the same partition so the next job starts from the checkpoint.
Deletions are inferred only when a full crawl finished entirely (deletion_semantics=full_crawl_reconcile).

webhook_process: turn verified inbox rows into envelopes through the connector's WebhookReceiver, apply them
in delivery order, and drop events older than the stored version of the same object (out-of-order delivery).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from wayam_connectors.contracts.capabilities import Capability, SyncRequest, WebhookDelivery
from wayam_connectors.contracts.envelope import Operation, SourceEnvelope, canonical_checksum
from wayam_connectors.contracts.errors import ConnectorError, CursorExpiredError, PolicyError, UnsupportedOperationError
from wayam_connectors.contracts.identity import AdapterContext, JobGrant, TenantContext
from wayam_connectors.contracts.manifest import DeletionSemantics
from wayam_connectors.platform.jobs import LeasedJob
from wayam_connectors.platform.records import QuarantineItem
from wayam_connectors.platform.runtime import Runtime
from wayam_connectors.platform.security.redaction import redact


def _ctx_for_job(job: LeasedJob) -> TenantContext:
    return TenantContext(tenant_id=job.tenant_id, product=job.product, principal_id=f"job:{job.id}", roles=frozenset())


def _bound_adapter(rt: Runtime, job: LeasedJob, capability: Capability) -> tuple[Any, Any]:
    ctx = _ctx_for_job(job)
    rt.grants.require(ctx, job.connection_id, capability.value)
    conn = rt.connections.get_by_ids(job.tenant_id, job.connection_id)
    entry = rt.registry[conn["connector_id"]]
    if not entry.manifest.supports(capability):
        raise UnsupportedOperationError("connector does not declare capability", details={"capability": capability.value})
    secret = rt.credentials.get(job.tenant_id, job.connection_id) if entry.manifest.auth_modes != ["inherited"] else {}
    context = AdapterContext(tenant_id=job.tenant_id, connection_id=job.connection_id, product=job.product)
    return entry, rt.adapter_for(entry, conn["config"], secret, context=context)


def record_sync(rt: Runtime, job: LeasedJob, grant: JobGrant) -> dict[str, Any]:
    entry, adapter = _bound_adapter(rt, job, Capability.RECORD_SYNC)
    manifest = entry.manifest
    payload = job.payload
    page_size = int(payload.get("page_size", 100))
    overlap = int(payload.get("overlap_seconds", 0))
    max_pages = int(payload.get("max_pages", 20))
    full_crawl = bool(payload.get("full_crawl", False))
    resource_type, resource_id = job.resource_type, job.resource_id
    checkpoint = rt.records.get_checkpoint(job.tenant_id, job.connection_id, resource_type, resource_id)
    cursor = checkpoint["cursor"] if checkpoint else None
    crawl_id = uuid.UUID(str(payload["crawl_id"])) if payload.get("crawl_id") else None
    if cursor and checkpoint and checkpoint.get("cursor_version") not in (None, manifest.cursor_version):
        cursor = None  # cursor format changed with the adapter: documented resync from the start
    if full_crawl and crawl_id is None:
        crawl_id = uuid.uuid4()
        cursor = None
        rt.records.clear_stale_crawls(job.tenant_id, job.connection_id)
    totals = {"pages": 0, "inserted": 0, "replayed": 0, "deleted": 0, "quarantined": 0, "partial_failures": 0}
    complete = False
    for _ in range(max_pages):
        if not rt.jobs.heartbeat(job):
            return {
                **totals,
                "complete": False,
                "stopped": "lease lost or cancel requested",
                "crawl_id": str(crawl_id) if crawl_id else None,
            }
        try:
            page = asyncio.run(
                adapter.sync_page(
                    SyncRequest(
                        resource_type=resource_type, resource_id=resource_id, cursor=cursor, page_size=page_size, overlap_seconds=overlap
                    )
                )
            )
        except CursorExpiredError as exc:
            # documented resync: drop the cursor, record why, and let the retry start over
            rt.records.commit_page(
                job.tenant_id,
                job.connection_id,
                resource_type=resource_type,
                resource_id=resource_id,
                envelopes=[],
                quarantine=[
                    QuarantineItem(provenance=f"{resource_type}:{resource_id}", reason="cursor_expired", error=redact(exc.to_dict()))
                ],
                next_cursor=None,
                cursor_version=manifest.cursor_version,
                job_id=job.id,
            )
            raise
        quarantine = [
            QuarantineItem(
                provenance=f"{resource_type}:{resource_id}:{f.get('id')}",
                reason="partial_failure",
                error=redact(f.get("error", f)),
                source_object_id=str(f.get("id")) if f.get("id") is not None else None,
            )
            for f in page.partial_failures
        ]
        stats = rt.records.commit_page(
            job.tenant_id,
            job.connection_id,
            resource_type=resource_type,
            resource_id=resource_id,
            envelopes=page.records,
            quarantine=quarantine,
            next_cursor=page.next_cursor,
            cursor_version=manifest.cursor_version,
            job_id=job.id,
            mark_success=page.complete,
            crawl_id=crawl_id,
            seen=[(e.source_object_type, e.source_object_id) for e in page.records] if crawl_id else None,
        )
        totals["pages"] += 1
        totals["inserted"] += stats.inserted
        totals["replayed"] += stats.replayed
        totals["deleted"] += stats.deleted
        totals["quarantined"] += stats.quarantined
        totals["partial_failures"] += len(page.partial_failures)
        cursor = page.next_cursor
        if page.complete:
            complete = True
            break
    result: dict[str, Any] = {**totals, "complete": complete, "crawl_id": str(crawl_id) if crawl_id else None}
    if not complete:
        result["resume"] = True
        return result
    if crawl_id and manifest.deletion_semantics == DeletionSemantics.FULL_CRAWL_RECONCILE:
        result["inferred_deletes"] = _reconcile_deletes(
            rt, job, crawl_id, page.records, resource_type, resource_id, manifest.cursor_version
        )
        rt.records.clear_crawl(job.tenant_id, job.connection_id, crawl_id)
    elif crawl_id:
        result["inferred_deletes"] = 0
        result["note"] = f"deletion_semantics={manifest.deletion_semantics.value}: nothing inferred from absence"
        rt.records.clear_crawl(job.tenant_id, job.connection_id, crawl_id)
    return result


def _reconcile_deletes(
    rt: Runtime,
    job: LeasedJob,
    crawl_id: uuid.UUID,
    sample: list[SourceEnvelope],
    resource_type: str,
    resource_id: str | None,
    cursor_version: str | None,
) -> int:
    """After a COMPLETE crawl: every current object of the crawled types not seen is deleted."""
    types = {e.source_object_type for e in sample} or {f"{resource_type}"}
    observed = datetime.now(UTC)
    total = 0
    for otype in sorted(types):
        missing = rt.records.unseen_current(job.tenant_id, job.connection_id, crawl_id, otype)
        if not missing:
            continue
        envs = [
            SourceEnvelope(
                tenant_id=job.tenant_id,
                product=job.product.value,
                connection_id=job.connection_id,
                source_system=rt.connections.get_by_ids(job.tenant_id, job.connection_id)["connector_id"],
                source_object_id=oid,
                source_object_type=otype,
                schema_version="reconcile/1",
                source_version=None,
                event_id=f"crawl:{crawl_id}:delete:{oid}",
                operation=Operation.DELETE,
                observed_at=observed,
                checksum=canonical_checksum({"deleted": oid}),
                provenance=f"full-crawl {crawl_id}: not present in complete listing",
                payload={"inferred": True},
            )
            for oid in missing
        ]
        stats = rt.records.commit_page(
            job.tenant_id,
            job.connection_id,
            resource_type=resource_type,
            resource_id=resource_id,
            envelopes=envs,
            next_cursor=None,
            cursor_version=cursor_version,
            job_id=job.id,
            outbox_topic="records.deleted",
        )
        total += stats.inserted
    return total


def _version_key(version: str | None, timestamp: datetime | None) -> tuple[int, str]:
    if timestamp is not None:
        return (1, timestamp.isoformat())
    return (0, version or "")


def webhook_process(rt: Runtime, job: LeasedJob, grant: JobGrant) -> dict[str, Any]:
    entry, adapter = _bound_adapter(rt, job, Capability.WEBHOOK_RECEIVER)
    pending = rt.webhooks.pending(job.tenant_id, job.connection_id, limit=int(job.payload.get("batch", 200)))
    applied = stale = rejected = 0
    for row in pending:
        delivery = WebhookDelivery(headers=row["headers"], body=bytes(row["body"]), received_at=row["received_at"])
        try:
            envelopes = adapter.envelopes_from_delivery(delivery)
        except (PolicyError, ConnectorError) as exc:
            rt.records.commit_page(
                job.tenant_id,
                job.connection_id,
                resource_type="webhook",
                resource_id=row["delivery_id"],
                envelopes=[],
                quarantine=[QuarantineItem(provenance=f"webhook:{row['delivery_id']}", reason="rejected", error=redact(exc.to_dict()))],
                next_cursor=None,
                cursor_version=None,
                job_id=job.id,
            )
            rt.webhooks.mark(job.tenant_id, row["id"], "rejected")
            rejected += 1
            continue
        fresh: list[SourceEnvelope] = []
        by_type: dict[str, list[SourceEnvelope]] = {}
        for e in envelopes:
            by_type.setdefault(e.source_object_type, []).append(e)
        for otype, envs in by_type.items():
            current = rt.records.current_versions(job.tenant_id, job.connection_id, otype, [e.source_object_id for e in envs])
            for e in envs:
                cur = current.get(e.source_object_id)
                if cur and _version_key(e.source_version, e.source_timestamp) < _version_key(
                    cur["source_version"], cur["source_timestamp"]
                ):
                    stale += 1  # an older state of the object arrived after a newer one: ignore (T-26)
                    continue
                fresh.append(e)
        rt.records.commit_page(
            job.tenant_id,
            job.connection_id,
            resource_type="webhook",
            resource_id=row["delivery_id"],
            envelopes=fresh,
            next_cursor=None,
            cursor_version=None,
            job_id=job.id,
            outbox_topic="records.committed",
        )
        rt.webhooks.mark(job.tenant_id, row["id"], "processed")
        applied += len(fresh)
    return {"deliveries": len(pending), "applied": applied, "stale_ignored": stale, "rejected": rejected}
