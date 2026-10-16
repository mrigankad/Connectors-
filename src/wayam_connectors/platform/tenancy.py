"""Tenants, API keys and capability grants. Tenant identity comes only from here."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.errors import AuthError, PolicyError
from wayam_connectors.contracts.identity import Product, TenantContext

from .db import Store

KEY_PREFIX = "wck_"


def _hash_key(raw: str) -> bytes:
    return hashlib.sha256(raw.encode()).digest()


class TenantService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def create_tenant(self, name: str) -> UUID:
        with self.store.bypass_tx("admin") as cur:
            cur.execute("INSERT INTO tenant (name) VALUES (%s) RETURNING id", (name,))
            return UUID(str(cur.fetchone()["id"]))  # type: ignore[index]

    def issue_api_key(self, tenant_id: UUID, product: Product, roles: list[str] | None = None) -> str:
        """Returns the raw key once. Only its sha256 is stored."""
        raw = KEY_PREFIX + secrets.token_urlsafe(32)
        with self.store.bypass_tx("admin") as cur:
            cur.execute(
                "INSERT INTO api_key (tenant_id, product, key_hash, key_prefix, roles) VALUES (%s, %s, %s, %s, %s)",
                (tenant_id, product.value, _hash_key(raw), raw[:8], roles or []),
            )
        return raw

    def revoke_api_key(self, tenant_id: UUID, key_prefix: str) -> int:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "UPDATE api_key SET revoked_at = now() WHERE tenant_id=%s AND key_prefix=%s AND revoked_at IS NULL",
                (tenant_id, key_prefix),
            )
            return cur.rowcount

    def authenticate(self, raw_key: str | None) -> TenantContext:
        """Resolve a bearer API key to a TenantContext. Constant-time compare on the stored hash."""
        if not raw_key or not raw_key.startswith(KEY_PREFIX):
            raise AuthError("missing or malformed API key")
        digest = _hash_key(raw_key)
        with self.store.bypass_tx("auth") as cur:
            cur.execute(
                "SELECT id, tenant_id, product, key_hash, roles FROM api_key WHERE key_hash = %s AND revoked_at IS NULL",
                (digest,),
            )
            row = cur.fetchone()
        if row is None or not hmac.compare_digest(bytes(row["key_hash"]), digest):
            raise AuthError("invalid API key")
        return TenantContext(
            tenant_id=UUID(str(row["tenant_id"])),
            product=Product(row["product"]),
            principal_id=f"api_key:{row['id']}",
            roles=frozenset(row["roles"] or []),
        )


class GrantService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def grant(
        self,
        ctx: TenantContext,
        connection_id: UUID,
        product: Product,
        capabilities: list[str],
        resource_allowlist: list[dict[str, Any]] | None = None,
    ) -> UUID:
        if "admin" not in ctx.roles:
            raise PolicyError("only tenant admins may grant capabilities")
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                "UPDATE capability_grant SET revoked_at = now() "
                "WHERE tenant_id=%s AND connection_id=%s AND product=%s AND revoked_at IS NULL",
                (ctx.tenant_id, connection_id, product.value),
            )
            cur.execute(
                "INSERT INTO capability_grant (tenant_id, connection_id, product, capabilities, resource_allowlist, granted_by) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (ctx.tenant_id, connection_id, product.value, capabilities, json.dumps(resource_allowlist or []), ctx.principal_id),
            )
            return UUID(str(cur.fetchone()["id"]))  # type: ignore[index]

    def revoke(self, ctx: TenantContext, connection_id: UUID, product: Product) -> int:
        if "admin" not in ctx.roles:
            raise PolicyError("only tenant admins may revoke capabilities")
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                "UPDATE capability_grant SET revoked_at = now() "
                "WHERE tenant_id=%s AND connection_id=%s AND product=%s AND revoked_at IS NULL",
                (ctx.tenant_id, connection_id, product.value),
            )
            return cur.rowcount

    def require(self, ctx: TenantContext, connection_id: UUID, capability: str) -> dict[str, Any]:
        """Check that ctx.product holds `capability` on the connection in ctx's tenant. Raises PolicyError otherwise.

        A guessed connection id from another tenant produces the same error as a missing grant:
        no existence disclosure.
        """
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                "SELECT g.capabilities, g.resource_allowlist, c.status FROM capability_grant g "
                "JOIN connection c ON c.tenant_id = g.tenant_id AND c.id = g.connection_id "
                "WHERE g.tenant_id=%s AND g.connection_id=%s AND g.product=%s AND g.revoked_at IS NULL",
                (ctx.tenant_id, connection_id, ctx.product.value),
            )
            row = cur.fetchone()
        if row is None or capability not in (row["capabilities"] or []):
            raise PolicyError("connection not found or capability not granted", details={"capability": capability})
        if row["status"] != "active":
            raise PolicyError("connection is not active", details={"status": row["status"]})
        return {"resource_allowlist": row["resource_allowlist"] or []}
