"""Durable job queue on PostgreSQL: leases, capped retries with jitter, dead-letter, replay, cancel."""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.errors import ConnectorError, ErrorClass, RateLimitedError, ValidationError
from wayam_connectors.contracts.identity import JobGrant, Product
from wayam_connectors.platform.security.redaction import redact

from .db import Store

TERMINAL = {"succeeded", "failed", "dead_letter", "cancelled"}


@dataclass(frozen=True)
class LeasedJob:
    id: UUID
    tenant_id: UUID
    connection_id: UUID
    product: Product
    job_type: str
    resource_type: str
    resource_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_token: UUID
    leased_until: datetime

    def grant(self, capabilities: list[str]) -> JobGrant:
        return JobGrant(
            tenant_id=self.tenant_id,
            product=self.product,
            connection_id=self.connection_id,
            job_id=self.id,
            capabilities=frozenset(capabilities),
            expires_at=self.leased_until,
        )


class JobQueue:
    def __init__(self, store: Store, *, lease_seconds: int = 300, base_backoff: float = 1.0, max_backoff: float = 120.0) -> None:
        self.store = store
        self.lease_seconds = lease_seconds
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff

    # ------------------------------------------------------------ producers

    def enqueue(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        product: Product,
        *,
        job_type: str,
        resource_type: str,
        resource_id: str | None = None,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 5,
        run_after: datetime | None = None,
    ) -> UUID:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT id FROM sync_job WHERE tenant_id=%s AND connection_id=%s AND job_type=%s AND resource_type=%s "
                "AND COALESCE(resource_id,'')=%s AND status IN ('queued','leased')",
                (tenant_id, connection_id, job_type, resource_type, resource_id or ""),
            )
            existing = cur.fetchone()
            if existing:
                raise ValidationError("a job for this partition is already active", details={"job_id": str(existing["id"])})
            cur.execute(
                "INSERT INTO sync_job (tenant_id, connection_id, product, job_type, resource_type, resource_id, "
                "payload, max_attempts, run_after) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    tenant_id,
                    connection_id,
                    product.value,
                    job_type,
                    resource_type,
                    resource_id,
                    json.dumps(payload or {}),
                    max_attempts,
                    run_after or datetime.now(UTC),
                ),
            )
            return UUID(str(cur.fetchone()["id"]))  # type: ignore[index]

    # ------------------------------------------------------------ workers

    def claim(self, worker_id: str, *, tenant_id: UUID | None = None) -> LeasedJob | None:
        """Claim one runnable job (expired leases are reclaimed). SKIP LOCKED makes this safe for many workers.

        Without `tenant_id` the worker serves every tenant; with it, only that tenant's partition (dedicated worker pools).
        """
        token = uuid.uuid4()
        until = datetime.now(UTC) + timedelta(seconds=self.lease_seconds)
        with self.store.bypass_tx("worker_claim") as cur:
            cur.execute(
                """
                WITH candidate AS (
                    SELECT id FROM sync_job
                    WHERE run_after <= now() AND cancel_requested = false
                      AND (status = 'queued' OR (status = 'leased' AND leased_until < now()))
                      AND (%s::uuid IS NULL OR tenant_id = %s::uuid)
                    ORDER BY run_after
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE sync_job j SET status='leased', lease_token=%s, leased_by=%s, leased_until=%s,
                    attempts = j.attempts + 1, started_at = COALESCE(j.started_at, now())
                FROM candidate WHERE j.id = candidate.id
                RETURNING j.id, j.tenant_id, j.connection_id, j.product, j.job_type, j.resource_type, j.resource_id,
                          j.payload, j.attempts, j.max_attempts
                """,
                (tenant_id, tenant_id, token, worker_id, until),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return LeasedJob(
            id=UUID(str(row["id"])),
            tenant_id=UUID(str(row["tenant_id"])),
            connection_id=UUID(str(row["connection_id"])),
            product=Product(row["product"]),
            job_type=row["job_type"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            payload=row["payload"] or {},
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            lease_token=token,
            leased_until=until,
        )

    def heartbeat(self, job: LeasedJob) -> bool:
        """Extend the lease. Returns False if the lease was lost or cancellation was requested (stop work)."""
        until = datetime.now(UTC) + timedelta(seconds=self.lease_seconds)
        with self.store.tenant_tx(job.tenant_id) as cur:
            cur.execute(
                "UPDATE sync_job SET leased_until=%s WHERE id=%s AND tenant_id=%s AND lease_token=%s "
                "AND status='leased' AND cancel_requested=false",
                (until, job.id, job.tenant_id, job.lease_token),
            )
            return cur.rowcount == 1

    def complete(self, job: LeasedJob, result: dict[str, Any]) -> bool:
        with self.store.tenant_tx(job.tenant_id) as cur:
            cur.execute(
                "UPDATE sync_job SET status='succeeded', result=%s, finished_at=now(), lease_token=NULL "
                "WHERE id=%s AND tenant_id=%s AND lease_token=%s AND status='leased'",
                (json.dumps(redact(result), default=str), job.id, job.tenant_id, job.lease_token),
            )
            return cur.rowcount == 1

    def fail(self, job: LeasedJob, error: ConnectorError) -> str:
        """Record a failure. Returns the new status: queued (will retry), failed (non-retryable) or dead_letter."""
        err = redact(error.to_dict())
        if not error.retryable or error.error_class in (ErrorClass.AUTH, ErrorClass.SCOPE, ErrorClass.POLICY):
            status = "failed"
            run_after = None
        elif job.attempts >= job.max_attempts:
            status = "dead_letter"
            run_after = None
        else:
            status = "queued"
            delay = self.backoff_seconds(job.attempts)
            if isinstance(error, RateLimitedError) and error.retry_after_seconds is not None:
                delay = max(delay, float(error.retry_after_seconds))
            run_after = datetime.now(UTC) + timedelta(seconds=delay)
        with self.store.tenant_tx(job.tenant_id) as cur:
            cur.execute(
                "UPDATE sync_job SET status=%s, last_error=%s, run_after=COALESCE(%s, run_after), lease_token=NULL, "
                "finished_at = CASE WHEN %s IN ('failed','dead_letter') THEN now() END "
                "WHERE id=%s AND tenant_id=%s AND lease_token=%s AND status='leased'",
                (status, json.dumps(err, default=str), run_after, status, job.id, job.tenant_id, job.lease_token),
            )
            if cur.rowcount != 1:
                return "lease_lost"
            if error.error_class in (ErrorClass.AUTH, ErrorClass.SCOPE):
                cur.execute(
                    "UPDATE connection SET status='blocked', status_reason=%s, updated_at=now() "
                    "WHERE tenant_id=%s AND id=%s AND status='active'",
                    (f"{error.error_class.value}: {error.message}"[:500], job.tenant_id, job.connection_id),
                )
        return status

    def backoff_seconds(self, attempt: int) -> float:
        raw = min(self.max_backoff, self.base_backoff * (2 ** max(attempt - 1, 0)))
        return float(raw * (0.5 + random.random()))  # noqa: S311 - jitter, not security

    # ------------------------------------------------------------ control

    def cancel(self, tenant_id: UUID, job_id: UUID) -> bool:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "UPDATE sync_job SET cancel_requested=true, status = CASE WHEN status='queued' THEN 'cancelled' ELSE status END, "
                "finished_at = CASE WHEN status='queued' THEN now() ELSE finished_at END "
                "WHERE tenant_id=%s AND id=%s AND status IN ('queued','leased')",
                (tenant_id, job_id),
            )
            return cur.rowcount == 1

    def replay(self, tenant_id: UUID, job_id: UUID) -> bool:
        """Put a dead-lettered/failed/cancelled job back in the queue with a fresh attempt budget."""
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "UPDATE sync_job SET status='queued', attempts=0, run_after=now(), cancel_requested=false, "
                "finished_at=NULL, lease_token=NULL "
                "WHERE tenant_id=%s AND id=%s AND status IN ('dead_letter','failed','cancelled')",
                (tenant_id, job_id),
            )
            return cur.rowcount == 1

    def get(self, tenant_id: UUID, job_id: UUID) -> dict[str, Any] | None:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT id, connection_id, product, job_type, resource_type, resource_id, status, attempts, max_attempts, run_after, "
                "leased_by, leased_until, last_error, result, created_at, started_at, finished_at, cancel_requested "
                "FROM sync_job WHERE tenant_id=%s AND id=%s",
                (tenant_id, job_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def list(self, tenant_id: UUID, connection_id: UUID | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.tenant_tx(tenant_id) as cur:
            if connection_id is None:
                cur.execute(
                    "SELECT id, connection_id, job_type, resource_type, resource_id, status, attempts, created_at, finished_at "
                    "FROM sync_job WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, limit),
                )
            else:
                cur.execute(
                    "SELECT id, connection_id, job_type, resource_type, resource_id, status, attempts, created_at, finished_at "
                    "FROM sync_job WHERE tenant_id=%s AND connection_id=%s ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, connection_id, limit),
                )
            return [dict(r) for r in cur.fetchall()]
