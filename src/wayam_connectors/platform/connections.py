"""Connection registry rows: a tenant's configured instance of a connector."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.errors import PolicyError, ValidationError
from wayam_connectors.contracts.identity import TenantContext

from .db import Store


class ConnectionService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def create(self, ctx: TenantContext, connector_id: str, name: str, config: dict[str, Any]) -> UUID:
        if "admin" not in ctx.roles:
            raise PolicyError("only tenant admins may create connections")
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute("SELECT 1 FROM connector_definition WHERE connector_id=%s", (connector_id,))
            if cur.fetchone() is None:
                raise ValidationError("unknown connector", details={"connector_id": connector_id})
            cur.execute(
                "INSERT INTO connection (tenant_id, connector_id, name, config) VALUES (%s, %s, %s, %s) RETURNING id",
                (ctx.tenant_id, connector_id, name, json.dumps(config)),
            )
            cid = UUID(str(cur.fetchone()["id"]))  # type: ignore[index]
            cur.execute(
                "INSERT INTO audit_event (tenant_id, actor, action, target, details) VALUES (%s, %s, 'connection.create', %s, %s)",
                (ctx.tenant_id, ctx.principal_id, str(cid), json.dumps({"connector_id": connector_id})),
            )
            return cid

    def get(self, ctx: TenantContext, connection_id: UUID) -> dict[str, Any]:
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                "SELECT id, connector_id, name, config, status, status_reason, created_at, updated_at FROM connection "
                "WHERE tenant_id=%s AND id=%s",
                (ctx.tenant_id, connection_id),
            )
            row = cur.fetchone()
        if row is None:
            raise PolicyError("connection not found")
        return dict(row)

    def list(self, ctx: TenantContext) -> list[dict[str, Any]]:
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                "SELECT id, connector_id, name, status, status_reason, created_at, updated_at FROM connection "
                "WHERE tenant_id=%s ORDER BY created_at",
                (ctx.tenant_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_by_ids(self, tenant_id: UUID, connection_id: UUID) -> dict[str, Any]:
        """Runtime-internal lookup for a job that already carries a validated tenant grant."""
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT id, connector_id, name, config, status, status_reason FROM connection WHERE tenant_id=%s AND id=%s",
                (tenant_id, connection_id),
            )
            row = cur.fetchone()
        if row is None:
            raise PolicyError("connection not found")
        return dict(row)

    def set_status(self, tenant_id: UUID, connection_id: UUID, status: str, reason: str) -> None:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "UPDATE connection SET status=%s, status_reason=%s, updated_at=now() WHERE tenant_id=%s AND id=%s",
                (status, reason, tenant_id, connection_id),
            )
