"""GitHub Actions: workflow runs, jobs, artifacts and redacted logs. Read-only; dispatch/rerun are not implemented."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from wayam_connectors.contracts.capabilities import CheckResult, DocumentContent, SyncPage, SyncRequest
from wayam_connectors.contracts.envelope import Operation, SourceEnvelope, canonical_checksum
from wayam_connectors.contracts.errors import DataError, PolicyError, UnsupportedOperationError, ValidationError
from wayam_connectors.contracts.identity import AdapterContext
from wayam_connectors.mappings.engineering.github import MAPPING_VERSION, map_artifact, map_workflow_job, map_workflow_run
from wayam_connectors.platform.security.files import DEFAULT_LIMITS, FileLimits, inspect_zip
from wayam_connectors.platform.security.redaction import redact_text

from .._core import GitHubClient, validate_config, validate_secret

ADAPTER_VERSION = "0.1.0"
CONNECTOR_ID = "github.actions"
CURSOR_VERSION = "1"
RESOURCE_TYPES = ("workflow_run", "workflow_job", "artifact")


class GitHubActions:
    CONNECTOR_ID = CONNECTOR_ID
    ADAPTER_VERSION = ADAPTER_VERSION

    def __init__(
        self,
        config: Mapping[str, Any],
        secret: Mapping[str, Any],
        context: AdapterContext | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve_dns: bool | None = None,
        limits: FileLimits = DEFAULT_LIMITS,
    ) -> None:
        self.config = validate_config(config)
        self.context = context
        self.limits = limits
        self.client = GitHubClient(self.config, validate_secret(secret), context, transport=transport, resolve_dns=resolve_dns)

    async def check_connection(self) -> CheckResult:
        counts = {}
        for full in self.config.repositories:
            owner, name = self.client.require_repo(full)
            data, _ = await self.client.get_json(f"/repos/{owner}/{name}/actions/workflows", {"per_page": 1})
            counts[full] = data.get("total_count")
        return CheckResult(
            ok=True,
            identity=self.config.auth_mode,
            edition=f"REST {self.config.api_base}",
            detail=f"actions read confirmed; workflows per repository: {counts}",
            checked_at=datetime.now(UTC),
        )

    async def sync_page(self, request: SyncRequest) -> SyncPage:
        if request.resource_type not in RESOURCE_TYPES:
            raise UnsupportedOperationError("unsupported resource type", details={"supported": list(RESOURCE_TYPES)})
        cursor = request.cursor or {}
        if cursor and cursor.get("v") != 1:
            raise ValidationError("unsupported cursor version")
        observed = datetime.now(UTC)
        iso = observed.isoformat()
        if request.resource_type == "workflow_run":
            owner, name = self.client.require_repo(request.resource_id)
            repo, _ = await self.client.get_json(f"/repos/{owner}/{name}")
            repo_id = str(repo["id"])
            params: dict[str, Any] = {"per_page": request.page_size}
            since = cursor.get("since")
            since_dt = datetime.fromisoformat(since) - timedelta(seconds=request.overlap_seconds) if since else None
            if since_dt:
                params["created"] = ">=" + since_dt.date().isoformat()  # date-granular; the overlap filter below is exact
            records, failures, next_url = [], [], None
            data: Any = {}
            max_seen = _dt(cursor.get("max_seen")) or (datetime.fromisoformat(since) if since else None)
            async for data, nxt in self.client.paginate(
                f"/repos/{owner}/{name}/actions/runs", params, start_url=cursor.get("next_url"), max_pages=1
            ):
                next_url = nxt
                for raw in data.get("workflow_runs", []):
                    try:
                        rec = map_workflow_run(raw, repo_id, iso)
                    except DataError as exc:
                        failures.append({"id": raw.get("id"), "error": exc.to_dict()})
                        continue
                    stamp = _dt(raw.get("updated_at"))
                    if since_dt and stamp and stamp < since_dt:
                        continue
                    records.append(
                        self._envelope(
                            "workflow_run",
                            rec.source_object_id,
                            rec.source_version,
                            rec.model_dump(mode="json"),
                            raw,
                            raw.get("url") or "",
                            observed,
                        )
                    )
                    if stamp and (max_seen is None or stamp > max_seen):
                        max_seen = stamp
            complete = next_url is None
            return SyncPage(
                records=records,
                next_cursor={
                    "v": 1,
                    "next_url": next_url,
                    "since": (max_seen.isoformat() if complete and max_seen else since),
                    "max_seen": (None if complete else (max_seen.isoformat() if max_seen else None)),
                },
                complete=complete,
                partial_failures=failures,
                source_counts={"workflow_run": len(records), "total_count": data.get("total_count")},
            )
        # jobs and artifacts are per run: resource_id = "owner/name#<run_id>"
        full, _, run_id = (request.resource_id or "").partition("#")
        owner, name = self.client.require_repo(full)
        if not run_id.isdigit():
            raise ValidationError("resource_id must be owner/name#<run_id> for jobs/artifacts")
        key = "jobs" if request.resource_type == "workflow_job" else "artifacts"
        records, failures, next_url = [], [], None
        data = {}
        async for data, nxt in self.client.paginate(
            f"/repos/{owner}/{name}/actions/runs/{run_id}/{key}",
            {"per_page": request.page_size},
            start_url=cursor.get("next_url"),
            max_pages=1,
        ):
            next_url = nxt
            for raw in data.get(key, []):
                try:
                    rec_any = map_workflow_job(raw, iso) if key == "jobs" else map_artifact(raw, run_id, iso)
                except DataError as exc:
                    failures.append({"id": raw.get("id"), "error": exc.to_dict()})
                    continue
                records.append(
                    self._envelope(
                        request.resource_type,
                        rec_any.source_object_id,
                        rec_any.source_version,
                        rec_any.model_dump(mode="json"),
                        raw,
                        raw.get("url") or "",
                        observed,
                    )
                )
        return SyncPage(
            records=records,
            next_cursor={"v": 1, "next_url": next_url},
            complete=next_url is None,
            partial_failures=failures,
            source_counts={request.resource_type: len(records), "total_count": data.get("total_count")},
        )

    async def fetch_document(self, resource_id: str, version_id: str | None = None) -> DocumentContent:
        """Run logs: `owner/name#run:<id>` -> redacted plain text concatenation of the log zip members."""
        full, _, rest = resource_id.partition("#")
        owner, name = self.client.require_repo(full)
        if not rest.startswith("run:") or not rest[4:].isdigit():
            raise ValidationError("resource_id must be owner/name#run:<run_id>")
        run_id = rest[4:]
        resp = await self.client.http.request(
            "GET", self.client.url(f"/repos/{owner}/{name}/actions/runs/{run_id}/logs"), allow_redirect_to=self.client.log_policy
        )
        zf = inspect_zip(resp.content, self.limits)
        parts: list[str] = []
        total = 0
        for info in sorted(zf.infolist(), key=lambda i: i.filename):
            if not info.filename.lower().endswith(".txt"):
                continue
            with zf.open(info) as fh:
                text = fh.read().decode("utf-8", errors="replace")
            total += len(text)
            if total > self.limits.max_text_chars:
                raise PolicyError("log text exceeds the configured limit")
            parts.append(f"===== {info.filename} =====\n{redact_text(text)}")
        content = "\n".join(parts).encode()
        return DocumentContent(content=content, content_type="text/plain; charset=utf-8", size=len(content), etag=None, version_id=run_id)

    def _envelope(
        self, rtype: str, oid: str, version: str | None, payload: dict[str, Any], raw: dict[str, Any], provenance: str, observed: datetime
    ) -> SourceEnvelope:
        if self.context is None:
            raise PolicyError("adapter is not bound to a tenant/connection context")
        return SourceEnvelope(
            tenant_id=self.context.tenant_id,
            product=self.context.product.value,
            connection_id=self.context.connection_id,
            source_system=CONNECTOR_ID,
            source_edition=f"REST {self.config.api_base}",
            source_object_id=oid,
            source_object_type=f"engineering.{rtype}",
            schema_version="engineering/1",
            mapping_version=MAPPING_VERSION,
            source_version=version,
            operation=Operation.UPSERT,
            source_timestamp=_dt(payload.get("updated_at_iso") or payload.get("completed_at_iso")),
            observed_at=observed,
            checksum=canonical_checksum(payload),
            provenance=provenance,
            payload=payload,
            extensions={"github": raw},
        )


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


ADAPTER = GitHubActions
