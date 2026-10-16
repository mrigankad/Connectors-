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

from wayam_connectors.contracts.capabilities import Capability, ParseResult, SyncRequest, WebhookDelivery
from wayam_connectors.contracts.domains.knowledge import SourcePermission
from wayam_connectors.contracts.envelope import Operation, SourceEnvelope, canonical_checksum
from wayam_connectors.contracts.errors import ConnectorError, CursorExpiredError, PolicyError, UnsupportedOperationError
from wayam_connectors.contracts.identity import AdapterContext, JobGrant, TenantContext
from wayam_connectors.contracts.manifest import DeletionSemantics
from wayam_connectors.platform.jobs import LeasedJob
from wayam_connectors.platform.parsers import parser_for
from wayam_connectors.platform.records import QuarantineItem
from wayam_connectors.platform.runtime import PLATFORM_CONFIG_KEY, Runtime
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


def document_ingest(rt: Runtime, job: LeasedJob, grant: JobGrant) -> dict[str, Any]:
    """Bodham ingest: for every current knowledge.document of a DocumentFetch connection, fetch the bytes, parse them
    with the format adapter chosen by name/MIME, and commit fragments plus the connection-level ACL.

    * unchanged documents (fragments already stored for the document's current version) are skipped;
    * a new version replaces the previous fragments (old ones become delete records);
    * deleted documents get their fragments deleted and their ACL state marked deleted;
    * unsupported formats and parse failures are quarantined with a reason; the job still succeeds.
    ACL: sources with container-level ACLs (S3) grant every document to the principals listed in the job payload
    or the connection config `knowledge_acl_principals`; without any principal nothing is readable (fail closed).
    """
    entry, adapter = _bound_adapter(rt, job, Capability.DOCUMENT_FETCH)
    conn = rt.connections.get_by_ids(job.tenant_id, job.connection_id)
    platform_cfg = conn["config"].get(PLATFORM_CONFIG_KEY) or {}
    principals = list(job.payload.get("acl_principals") or platform_cfg.get("knowledge_acl_principals") or [])
    acl_version = f"connection:{hashlib_sha(','.join(sorted(principals)))}"
    limit = int(job.payload.get("limit", 500))
    docs = rt.records.current_records(job.tenant_id, job.connection_id, "knowledge.document", limit=limit, include_deleted=True)
    observed = datetime.now(UTC)
    stats = {"documents": len(docs), "ingested": 0, "unchanged": 0, "deleted": 0, "quarantined": 0, "fragments": 0}
    by_doc: dict[str, list[dict[str, Any]]] = {}
    for f in rt.records.current_records(job.tenant_id, job.connection_id, "knowledge.fragment", limit=100000):
        by_doc.setdefault(str(f["payload"].get("document_id")), []).append(f)
    for doc in docs:
        if not rt.jobs.heartbeat(job):
            return {**stats, "stopped": "lease lost or cancel requested"}
        key = doc["source_object_id"]
        version = doc["source_version"] or doc["checksum"]
        mine = by_doc.get(key, [])
        if doc["operation"] == "delete" or doc["payload"].get("deleted"):
            if mine:
                _delete_fragments(rt, job, mine, observed, reason=f"source document {key} deleted")
            rt.knowledge.mark_document_deleted(job.tenant_id, job.connection_id, key)
            stats["deleted"] += 1
            continue
        if mine and all(f["payload"].get("version_id") == version for f in mine):
            # still refresh the ACL evidence so the 15-minute window does not expire for unchanged documents
            rt.knowledge.replace_document_acl(
                job.tenant_id, job.connection_id, key, _perms(key, principals, acl_version, observed), acl_version=acl_version
            )
            stats["unchanged"] += 1
            continue
        parser_id = parser_for(key, None)
        try:
            content = asyncio.run(adapter.fetch_document(key))
            parser_id = parser_id or parser_for(key, content.content_type)
            if parser_id is None or parser_id not in rt.registry:
                raise UnsupportedOperationError("no parser for this document", details={"key": key, "content_type": content.content_type})
            result: ParseResult = rt.registry[parser_id].adapter_cls().parse(content.content, source_ref=doc["provenance"])
        except ConnectorError as exc:
            rt.records.commit_page(
                job.tenant_id,
                job.connection_id,
                resource_type="ingest",
                resource_id=key,
                envelopes=[],
                quarantine=[
                    QuarantineItem(provenance=doc["provenance"], reason="ingest", error=redact(exc.to_dict()), source_object_id=key)
                ],
                next_cursor=None,
                cursor_version=None,
                job_id=job.id,
            )
            stats["quarantined"] += 1
            continue
        envelopes = []
        for item in result.items:
            payload = {"document_id": key, "version_id": version, **item}
            envelopes.append(
                SourceEnvelope(
                    tenant_id=job.tenant_id,
                    product=job.product.value,
                    connection_id=job.connection_id,
                    source_system=parser_id,
                    source_edition=entry.manifest.edition,
                    source_object_id=f"{key}#{item['fragment_index']}",
                    source_object_type="knowledge.fragment",
                    schema_version="knowledge/1",
                    mapping_version="knowledge.fragment/1",
                    source_version=version,
                    operation=Operation.UPSERT,
                    observed_at=observed,
                    checksum=canonical_checksum(payload),
                    provenance=f"{doc['provenance']}#{item['anchor'].get('kind')}:{item['fragment_index']}",
                    payload=payload,
                    extensions={"parse": {"complete": result.complete, "issues": [i.model_dump() for i in result.issues]}},
                )
            )
        stale = [f for f in mine if f["payload"].get("version_id") != version]
        if stale:
            _delete_fragments(rt, job, stale, observed, reason=f"superseded by version {version}")
        rt.records.commit_page(
            job.tenant_id,
            job.connection_id,
            resource_type="ingest",
            resource_id=key,
            envelopes=envelopes,
            next_cursor=None,
            cursor_version=None,
            job_id=job.id,
            mark_success=True,
        )
        rt.knowledge.replace_document_acl(
            job.tenant_id, job.connection_id, key, _perms(key, principals, acl_version, observed), acl_version=acl_version
        )
        stats["ingested"] += 1
        stats["fragments"] += len(envelopes)
    return stats


def hashlib_sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _perms(document_id: str, principals: list[str], acl_version: str, observed: datetime) -> list[SourcePermission]:
    return [
        SourcePermission(
            source_system="connection",
            source_object_id=f"{document_id}:{p}",
            observed_at_iso=observed.isoformat(),
            document_id=document_id,
            principal_id=p,
            role="read",
            inherited=True,
            acl_version=acl_version,
        )
        for p in principals
    ]


def _delete_fragments(rt: Runtime, job: LeasedJob, fragments: list[dict[str, Any]], observed: datetime, *, reason: str) -> None:
    envs = [
        SourceEnvelope(
            tenant_id=job.tenant_id,
            product=job.product.value,
            connection_id=job.connection_id,
            source_system="ingest",
            source_object_id=f["source_object_id"],
            source_object_type="knowledge.fragment",
            schema_version="knowledge/1",
            source_version=None,
            event_id=f"ingest:{job.id}:delete:{f['source_object_id']}",
            operation=Operation.DELETE,
            observed_at=observed,
            checksum=canonical_checksum({"deleted": f["source_object_id"]}),
            provenance=reason,
            payload={"deleted": True},
        )
        for f in fragments
    ]
    rt.records.commit_page(
        job.tenant_id,
        job.connection_id,
        resource_type="ingest",
        resource_id=None,
        envelopes=envs,
        next_cursor=None,
        cursor_version=None,
        job_id=job.id,
        outbox_topic="records.deleted",
    )
