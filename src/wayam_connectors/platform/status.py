"""Operational status per connection: freshness, lag, counts and partial-failure reasons, from stored state only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.identity import TenantContext

from .connections import ConnectionService
from .db import Store


class StatusService:
    def __init__(self, store: Store, connections: ConnectionService) -> None:
        self.store = store
        self.connections = connections

    def connection_status(self, ctx: TenantContext, connection_id: UUID, *, now: datetime | None = None) -> dict[str, Any]:
        conn = self.connections.get(ctx, connection_id)
        now = now or datetime.now(UTC)
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                "SELECT resource_type, resource_id, cursor IS NOT NULL AS has_cursor, cursor_version, "
                "last_success_at, records_total, updated_at "
                "FROM checkpoint WHERE tenant_id=%s AND connection_id=%s ORDER BY resource_type, resource_id",
                (ctx.tenant_id, connection_id),
            )
            partitions = []
            for r in cur.fetchall():
                lag = (now - r["last_success_at"]).total_seconds() if r["last_success_at"] else None
                partitions.append(
                    {
                        "resource_type": r["resource_type"],
                        "resource_id": r["resource_id"] or None,
                        "last_success_at": _iso(r["last_success_at"]),
                        "lag_seconds": round(lag, 1) if lag is not None else None,
                        "records_total": r["records_total"],
                        "has_cursor": r["has_cursor"],
                        "cursor_version": r["cursor_version"],
                        "updated_at": _iso(r["updated_at"]),
                    }
                )
            cur.execute(
                "SELECT status, count(*) AS n FROM sync_job WHERE tenant_id=%s AND connection_id=%s GROUP BY status",
                (ctx.tenant_id, connection_id),
            )
            jobs = {r["status"]: r["n"] for r in cur.fetchall()}
            cur.execute(
                "SELECT id, job_type, resource_type, resource_id, status, attempts, last_error, finished_at FROM sync_job "
                "WHERE tenant_id=%s AND connection_id=%s AND status IN ('failed','dead_letter') ORDER BY "
                "finished_at DESC NULLS LAST LIMIT 5",
                (ctx.tenant_id, connection_id),
            )
            failures = [
                {
                    "job_id": str(r["id"]),
                    "job_type": r["job_type"],
                    "resource_type": r["resource_type"],
                    "resource_id": r["resource_id"],
                    "status": r["status"],
                    "attempts": r["attempts"],
                    "finished_at": _iso(r["finished_at"]),
                    "error": (r["last_error"] or {}).get("message"),
                    "error_class": (r["last_error"] or {}).get("class"),
                }
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT reason, count(*) AS n FROM quarantine_record WHERE tenant_id=%s AND connection_id=%s GROUP BY reason",
                (ctx.tenant_id, connection_id),
            )
            quarantine = {r["reason"]: r["n"] for r in cur.fetchall()}
            cur.execute(
                "SELECT status, count(*) AS n FROM webhook_inbox WHERE tenant_id=%s AND connection_id=%s GROUP BY status",
                (ctx.tenant_id, connection_id),
            )
            webhooks = {r["status"]: r["n"] for r in cur.fetchall()}
            cur.execute(
                "SELECT source_object_type, count(*) FILTER (WHERE operation <> 'delete') AS live, count(*) FILTER "
                "(WHERE operation = 'delete') AS deleted "
                "FROM normalized_record WHERE tenant_id=%s AND connection_id=%s AND is_current GROUP BY source_object_type ORDER BY 1",
                (ctx.tenant_id, connection_id),
            )
            records = {r["source_object_type"]: {"current": r["live"], "deleted": r["deleted"]} for r in cur.fetchall()}
            cur.execute(
                "SELECT count(*) AS n, min(confirmed_at) AS oldest FROM document_acl_state WHERE tenant_id=%s AND "
                "connection_id=%s AND deleted=false",
                (ctx.tenant_id, connection_id),
            )
            acl = cur.fetchone() or {}
        evaluated = bool(partitions or records)
        return {
            "connection_id": str(connection_id),
            "connector_id": conn["connector_id"],
            "status": conn["status"],
            "status_reason": conn["status_reason"],
            "evaluated": evaluated,
            "as_of": now.isoformat(),
            "partitions": partitions,
            "records": records,
            "jobs": jobs,
            "recent_failures": failures,
            "quarantine": quarantine,
            "webhooks": webhooks,
            "acl": {
                "documents": acl.get("n", 0),
                "oldest_evidence_at": _iso(acl.get("oldest")),
                "oldest_evidence_age_seconds": round((now - acl["oldest"]).total_seconds(), 1) if acl.get("oldest") else None,
            },
        }


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None
