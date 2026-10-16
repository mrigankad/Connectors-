"""Engineering lineage over stored records: commit -> pull requests -> workflow runs (-> deployments when present).

Everything is joined on immutable provider ids already in the records; nothing is fetched or guessed here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from wayam_connectors.contracts.errors import ValidationError
from wayam_connectors.contracts.identity import TenantContext

from .connections import ConnectionService
from .db import Store


class LineageService:
    def __init__(self, store: Store, connections: ConnectionService) -> None:
        self.store = store
        self.connections = connections

    def for_commit(self, ctx: TenantContext, sha: str, *, connection_ids: list[UUID] | None = None) -> dict[str, Any]:
        """Lineage across the tenant's connections (repositories and Actions may be separate connections)."""
        if not sha or len(sha) < 7 or not all(c in "0123456789abcdef" for c in sha.lower()):
            raise ValidationError("sha must be a hex commit id (7+ chars)")
        sha = sha.lower()
        with self.store.tenant_tx(ctx.tenant_id) as cur:
            params: list[Any] = [ctx.tenant_id, sha + "%"]
            conn_filter = ""
            if connection_ids:
                conn_filter = " AND connection_id = ANY(%s)"
                params.append(connection_ids)
            cur.execute(
                "SELECT connection_id, source_object_id, payload, provenance, observed_at FROM normalized_record "
                "WHERE tenant_id=%s AND is_current "
                "AND operation <> 'delete' AND source_object_type='engineering.commit' AND source_object_id LIKE %s" + conn_filter,
                params,
            )
            commits = [dict(r) for r in cur.fetchall()]
            if not commits:
                return {"sha": sha, "found": False, "commits": [], "pull_requests": [], "workflow_runs": [], "deployments": []}
            full_shas = sorted({c["source_object_id"] for c in commits})
            repo_ids = sorted({c["payload"].get("repository_id") for c in commits if c["payload"].get("repository_id")})
            cur.execute(
                "SELECT connection_id, payload, provenance FROM normalized_record WHERE tenant_id=%s AND is_current "
                "AND operation <> 'delete' "
                "AND source_object_type='engineering.pull_request' AND payload->>'head_sha' = ANY(%s)"
                + conn_filter.replace("connection_id", "connection_id"),
                [ctx.tenant_id, full_shas] + ([connection_ids] if connection_ids else []),
            )
            prs = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT connection_id, payload, provenance FROM normalized_record WHERE tenant_id=%s AND is_current "
                "AND operation <> 'delete' "
                "AND source_object_type='engineering.workflow_run' AND payload->>'head_sha' = ANY(%s)" + conn_filter,
                [ctx.tenant_id, full_shas] + ([connection_ids] if connection_ids else []),
            )
            runs = [dict(r) for r in cur.fetchall()]
            run_ids = [r["payload"]["run_id"] for r in runs]
            jobs: list[dict[str, Any]] = []
            if run_ids:
                cur.execute(
                    "SELECT payload FROM normalized_record WHERE tenant_id=%s AND is_current AND operation <> 'delete' "
                    "AND source_object_type='engineering.workflow_job' AND payload->>'run_id' = ANY(%s)",
                    (ctx.tenant_id, run_ids),
                )
                jobs = [dict(r)["payload"] for r in cur.fetchall()]
            cur.execute(
                "SELECT connection_id, payload, provenance FROM normalized_record WHERE tenant_id=%s AND is_current "
                "AND operation <> 'delete' "
                "AND source_object_type='engineering.deployment' AND payload->>'commit_sha' = ANY(%s)" + conn_filter,
                [ctx.tenant_id, full_shas] + ([connection_ids] if connection_ids else []),
            )
            deployments = [dict(r) for r in cur.fetchall()]
        by_run: dict[str, list[dict[str, Any]]] = {}
        for j in jobs:
            by_run.setdefault(j["run_id"], []).append(
                {"job_id": j["job_id"], "name": j["name"], "status": j["status"], "conclusion": j["conclusion"]}
            )
        return {
            "sha": sha,
            "found": True,
            "repository_ids": repo_ids,
            "commits": [
                {
                    "sha": c["source_object_id"],
                    "repository_id": c["payload"].get("repository_id"),
                    "message": c["payload"].get("message"),
                    "author": c["payload"].get("author"),
                    "authored_at": c["payload"].get("authored_at_iso"),
                    "provenance": c["provenance"],
                    "connection_id": str(c["connection_id"]),
                }
                for c in commits
            ],
            "pull_requests": [
                {
                    "number": p["payload"]["number"],
                    "title": p["payload"]["title"],
                    "state": p["payload"]["state"],
                    "merged_at": p["payload"].get("merged_at_iso"),
                    "provenance": p["provenance"],
                    "connection_id": str(p["connection_id"]),
                }
                for p in prs
            ],
            "workflow_runs": [
                {
                    "run_id": r["payload"]["run_id"],
                    "status": r["payload"]["status"],
                    "conclusion": r["payload"]["conclusion"],
                    "run_attempt": r["payload"].get("run_attempt"),
                    "updated_at": r["payload"].get("updated_at_iso"),
                    "provenance": r["provenance"],
                    "jobs": by_run.get(r["payload"]["run_id"], []),
                    "connection_id": str(r["connection_id"]),
                }
                for r in runs
            ],
            "deployments": [
                {
                    "deployment_id": d["payload"]["deployment_id"],
                    "state": d["payload"].get("state"),
                    "url": d["payload"].get("url"),
                    "environment": d["payload"].get("environment"),
                    "provenance": d["provenance"],
                    "connection_id": str(d["connection_id"]),
                }
                for d in deployments
            ],
            "evaluated": {"deployments": bool(deployments) or "no deployment source connected: not evaluated"},
        }
