"""Recurring schedules: a tick enqueues due jobs. Skipping is explicit: a partition with an active job is left alone
and the schedule moves to its next slot, so a slow sync never piles up duplicate jobs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.errors import PolicyError, ValidationError
from wayam_connectors.contracts.identity import Product, TenantContext

from .db import Store
from .jobs import JobQueue
from .tenancy import GrantService

JOB_CAPABILITY = {
    "record_sync": "RecordSync",
    "document_ingest": "DocumentFetch",
    "webhook_process": "WebhookReceiver",
    "connection_check": None,
}


class Scheduler:
    def __init__(self, store: Store, jobs: JobQueue, grants: GrantService) -> None:
        self.store = store
        self.jobs = jobs
        self.grants = grants

    def create(
        self,
        ctx: TenantContext,
        connection_id: UUID,
        *,
        job_type: str,
        resource_type: str,
        resource_id: str | None,
        interval_seconds: int,
        payload: dict[str, Any] | None = None,
        product: Product | None = None,
    ) -> UUID:
        if "admin" not in ctx.roles:
            raise PolicyError("only tenant admins may create schedules")
        if job_type not in JOB_CAPABILITY:
            raise ValidationError("unknown job_type", details={"known": sorted(JOB_CAPABILITY)})
        if interval_seconds < 60:
            raise ValidationError("interval_seconds must be >= 60")
        product = product or ctx.product
        cap = JOB_CAPABILITY[job_type]
        if cap:  # the product running the job must hold the capability on the connection
            self.grants.require(
                TenantContext(tenant_id=ctx.tenant_id, product=product, principal_id=ctx.principal_id, roles=ctx.roles), connection_id, cap
            )
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                "INSERT INTO sync_schedule (tenant_id, connection_id, product, job_type, resource_type, "
                "resource_id, payload, interval_seconds, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, connection_id, job_type, "
                "resource_type, COALESCE(resource_id, '')) "
                "DO UPDATE SET payload=EXCLUDED.payload, interval_seconds=EXCLUDED.interval_seconds, enabled=true, "
                "product=EXCLUDED.product RETURNING id",
                (
                    ctx.tenant_id,
                    connection_id,
                    product.value,
                    job_type,
                    resource_type,
                    resource_id,
                    json.dumps(payload or {}),
                    interval_seconds,
                    ctx.principal_id,
                ),
            )
            return UUID(str(cur.fetchone()["id"]))  # type: ignore[index]

    def list(self, ctx: TenantContext, connection_id: UUID) -> list[dict[str, Any]]:
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                "SELECT id, product, job_type, resource_type, resource_id, payload, interval_seconds, enabled, "
                "next_run_at, last_enqueued_at, last_job_id "
                "FROM sync_schedule WHERE tenant_id=%s AND connection_id=%s ORDER BY created_at",
                (ctx.tenant_id, connection_id),
            )
            return [dict(r) for r in cur.fetchall()]

    def set_enabled(self, ctx: TenantContext, schedule_id: UUID, enabled: bool) -> bool:
        if "admin" not in ctx.roles:
            raise PolicyError("only tenant admins may change schedules")
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute("UPDATE sync_schedule SET enabled=%s WHERE tenant_id=%s AND id=%s", (enabled, ctx.tenant_id, schedule_id))
            return cur.rowcount == 1

    def tick(self, *, now: datetime | None = None, limit: int = 100) -> dict[str, int]:
        """Enqueue every due schedule. Safe to run from many workers: due rows are locked with SKIP LOCKED."""
        now = now or datetime.now(UTC)
        stats = {"due": 0, "enqueued": 0, "skipped_active": 0, "skipped_blocked": 0}
        with self.store.bypass_tx("worker_claim") as cur:
            cur.execute(
                "SELECT s.id, s.tenant_id, s.connection_id, s.product, s.job_type, s.resource_type, s.resource_id, "
                "s.payload, s.interval_seconds, "
                "c.status AS connection_status FROM sync_schedule s JOIN connection c ON c.tenant_id = s.tenant_id "
                "AND c.id = s.connection_id "
                "WHERE s.enabled AND s.next_run_at <= %s ORDER BY s.next_run_at FOR UPDATE OF s SKIP LOCKED LIMIT %s",
                (now, limit),
            )
            due = [dict(r) for r in cur.fetchall()]
            for s in due:
                stats["due"] += 1
                next_run = now + timedelta(seconds=int(s["interval_seconds"]))
                if s["connection_status"] != "active":
                    stats["skipped_blocked"] += 1
                    cur.execute("UPDATE sync_schedule SET next_run_at=%s WHERE id=%s", (next_run, s["id"]))
                    continue
                cur.execute(
                    "SELECT 1 FROM sync_job WHERE tenant_id=%s AND connection_id=%s AND job_type=%s AND "
                    "resource_type=%s AND COALESCE(resource_id,'')=%s "
                    "AND status IN ('queued','leased')",
                    (s["tenant_id"], s["connection_id"], s["job_type"], s["resource_type"], s["resource_id"] or ""),
                )
                if cur.fetchone():
                    stats["skipped_active"] += 1
                    cur.execute("UPDATE sync_schedule SET next_run_at=%s WHERE id=%s", (next_run, s["id"]))
                    continue
                cur.execute(
                    "INSERT INTO sync_job (tenant_id, connection_id, product, job_type, resource_type, resource_id, "
                    "payload, max_attempts, run_after) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING id",
                    (
                        s["tenant_id"],
                        s["connection_id"],
                        s["product"],
                        s["job_type"],
                        s["resource_type"],
                        s["resource_id"],
                        json.dumps(s["payload"] or {}),
                        5,
                    ),
                )
                jid = cur.fetchone()["id"]  # type: ignore[index]
                cur.execute(
                    "UPDATE sync_schedule SET next_run_at=%s, last_enqueued_at=%s, last_job_id=%s WHERE id=%s",
                    (next_run, now, jid, s["id"]),
                )
                stats["enqueued"] += 1
        return stats
