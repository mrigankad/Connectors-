"""Knowledge access contract: source ACLs are enforced before any fragment reaches retrieval.

Rules (docs/architecture.md, "Authorization and knowledge"):
* The crawler's permission is not the user's permission. A product asks for fragments *as* a Wayam principal,
  which maps to a source principal per connection. Unknown principal -> nothing.
* A document is visible when an ACL row grants the principal or one of its (transitively expanded) groups,
  the ACL evidence for the document is fresher than `max_acl_age`, the document is not deleted, and the
  connection is active. Anything else fails closed.
* Revocation of the connection blocks immediately (status check). Group removal takes effect on the next
  principal sync; stale evidence beyond the window withholds content until revalidated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.domains.knowledge import SourcePermission
from wayam_connectors.contracts.errors import PolicyError
from wayam_connectors.contracts.identity import TenantContext

from .db import Store
from .tenancy import GrantService

DEFAULT_MAX_ACL_AGE = timedelta(minutes=15)
MAX_GROUP_DEPTH = 10


class KnowledgeAccessService:
    def __init__(self, store: Store, grants: GrantService, *, max_acl_age: timedelta = DEFAULT_MAX_ACL_AGE) -> None:
        self.store = store
        self.grants = grants
        self.max_acl_age = max_acl_age

    # ------------------------------------------------------------ writes (from PermissionSync / fixtures)

    def upsert_principals(self, tenant_id: UUID, connection_id: UUID, principals: list[dict[str, Any]]) -> int:
        with self.store.tenant_tx(tenant_id) as cur:
            for p in principals:
                cur.execute(
                    "INSERT INTO source_principal (tenant_id, connection_id, principal_id, kind, display_name, email, member_of, "
                    "observed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (tenant_id, connection_id, principal_id) DO UPDATE SET "
                    "kind=EXCLUDED.kind, display_name=EXCLUDED.display_name, email=EXCLUDED.email, member_of=EXCLUDED.member_of, "
                    "observed_at=now()",
                    (
                        tenant_id,
                        connection_id,
                        p["principal_id"],
                        p.get("kind", "unknown"),
                        p.get("display_name"),
                        p.get("email"),
                        list(p.get("member_of") or []),
                    ),
                )
        return len(principals)

    def replace_document_acl(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        document_id: str,
        permissions: list[SourcePermission],
        *,
        acl_version: str,
        confirmed_at: datetime | None = None,
        deleted: bool = False,
    ) -> None:
        """Replace the full ACL of one document atomically (partial ACL knowledge is never stored as complete)."""
        now = confirmed_at or datetime.now(UTC)
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "DELETE FROM source_acl WHERE tenant_id=%s AND connection_id=%s AND document_id=%s", (tenant_id, connection_id, document_id)
            )
            for perm in permissions:
                cur.execute(
                    "INSERT INTO source_acl (tenant_id, connection_id, document_id, principal_id, role, inherited, acl_version, "
                    "observed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (tenant_id, connection_id, document_id, perm.principal_id, perm.role, perm.inherited, acl_version, now),
                )
            cur.execute(
                "INSERT INTO document_acl_state (tenant_id, connection_id, document_id, acl_version, confirmed_at, deleted) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, connection_id, document_id) DO UPDATE SET "
                "acl_version=EXCLUDED.acl_version, confirmed_at=EXCLUDED.confirmed_at, deleted=EXCLUDED.deleted",
                (tenant_id, connection_id, document_id, acl_version, now, deleted),
            )

    def mark_document_deleted(self, tenant_id: UUID, connection_id: UUID, document_id: str) -> None:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "UPDATE document_acl_state SET deleted=true WHERE tenant_id=%s AND connection_id=%s AND document_id=%s",
                (tenant_id, connection_id, document_id),
            )
            cur.execute(
                "DELETE FROM source_acl WHERE tenant_id=%s AND connection_id=%s AND document_id=%s", (tenant_id, connection_id, document_id)
            )

    def map_principal(self, tenant_id: UUID, connection_id: UUID, wayam_principal: str, source_principal_id: str) -> None:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "INSERT INTO principal_mapping (tenant_id, connection_id, wayam_principal, source_principal_id) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, connection_id, wayam_principal) DO UPDATE SET source_principal_id=EXCLUDED.source_principal_id",
                (tenant_id, connection_id, wayam_principal, source_principal_id),
            )

    # ------------------------------------------------------------ reads

    def effective_principals(self, tenant_id: UUID, connection_id: UUID, source_principal_id: str) -> set[str]:
        """The principal plus every group reachable through member_of (bounded depth). Unknown principal -> empty set."""
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT principal_id, member_of FROM source_principal WHERE tenant_id=%s AND connection_id=%s", (tenant_id, connection_id)
            )
            members = {r["principal_id"]: list(r["member_of"] or []) for r in cur.fetchall()}
        if source_principal_id not in members:
            return set()
        seen = {source_principal_id}
        frontier = [source_principal_id]
        for _ in range(MAX_GROUP_DEPTH):
            nxt = [g for p in frontier for g in members.get(p, []) if g not in seen]
            if not nxt:
                break
            seen.update(nxt)
            frontier = nxt
        return seen

    def visible_fragments(
        self, ctx: TenantContext, connection_id: UUID, *, wayam_principal: str, limit: int = 200, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Fragments the given Wayam principal may read, with the ACL reason attached. Fails closed on every doubt."""
        # the product must hold a content-bearing grant on this connection (parsers for uploads, DocumentFetch for cloud sources)
        try:
            self.grants.require(ctx, connection_id, "FileParser")
        except PolicyError:
            self.grants.require(ctx, connection_id, "DocumentFetch")
        now = now or datetime.now(UTC)
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute("SELECT status FROM connection WHERE tenant_id=%s AND id=%s", (ctx.tenant_id, connection_id))
            row = cur.fetchone()
            if row is None or row["status"] != "active":
                raise PolicyError("connection is not active; knowledge access blocked")
            cur.execute(
                "SELECT source_principal_id FROM principal_mapping WHERE tenant_id=%s AND connection_id=%s AND wayam_principal=%s",
                (ctx.tenant_id, connection_id, wayam_principal),
            )
            m = cur.fetchone()
        if m is None:
            return []
        principals = self.effective_principals(ctx.tenant_id, connection_id, m["source_principal_id"])
        if not principals:
            return []
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                """
                SELECT r.source_object_id, r.payload, r.provenance, s.document_id, s.acl_version, s.confirmed_at,
                       a.principal_id AS granted_via, a.role
                FROM normalized_record r
                JOIN document_acl_state s ON s.tenant_id = r.tenant_id AND s.connection_id = r.connection_id
                     AND s.document_id = r.payload->>'document_id'
                JOIN source_acl a ON a.tenant_id = s.tenant_id AND a.connection_id = s.connection_id AND a.document_id = s.document_id
                WHERE r.tenant_id = %s AND r.connection_id = %s AND r.source_object_type = 'knowledge.fragment' AND r.is_current
                  AND r.operation <> 'delete' AND s.deleted = false AND s.confirmed_at >= %s
                  AND a.principal_id = ANY(%s)
                ORDER BY r.source_object_id
                LIMIT %s
                """,
                (ctx.tenant_id, connection_id, now - self.max_acl_age, sorted(principals), limit),
            )
            rows = cur.fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:  # one fragment may be granted through several principals; keep the first
            out.setdefault(
                r["source_object_id"],
                {
                    "fragment_id": r["source_object_id"],
                    "document_id": r["document_id"],
                    "text": r["payload"].get("text"),
                    "citation": r["payload"].get("citation"),
                    "anchor": r["payload"].get("anchor"),
                    "completeness": r["payload"].get("completeness"),
                    "provenance": r["provenance"],
                    "acl_version": r["acl_version"],
                    "acl_confirmed_at": r["confirmed_at"].isoformat(),
                    "granted_via": r["granted_via"],
                    "role": r["role"],
                },
            )
        return list(out.values())

    def stale_documents(self, tenant_id: UUID, connection_id: UUID, *, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT document_id FROM document_acl_state WHERE tenant_id=%s AND connection_id=%s AND deleted=false "
                "AND confirmed_at < %s ORDER BY 1",
                (tenant_id, connection_id, now - self.max_acl_age),
            )
            return [r["document_id"] for r in cur.fetchall()]
