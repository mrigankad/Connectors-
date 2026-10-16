"""GitHub repositories: allowlisted repositories, commits, pull requests, issues; webhook verification.

Read-only by construction: there is no method that writes, and the shared transport refuses bare
mutations. Cursor v1 = {"v": 1, "next_url": <page to resume> | null, "since": <ISO watermark>}.
Watermarks use an overlap window (SyncRequest.overlap_seconds) and the per-object `updated_at`
(or commit date) as the source version, so late updates become new versions rather than lost rows.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from wayam_connectors.contracts.capabilities import (
    CheckResult,
    Resource,
    SyncPage,
    SyncRequest,
    WebhookDelivery,
    WebhookVerdict,
)
from wayam_connectors.contracts.envelope import Operation, SourceEnvelope, canonical_checksum
from wayam_connectors.contracts.errors import DataError, PolicyError, UnsupportedOperationError, ValidationError
from wayam_connectors.contracts.identity import AdapterContext
from wayam_connectors.mappings.engineering.github import (
    MAPPING_VERSION,
    map_commit,
    map_issue,
    map_pull_request,
    map_repository,
)

from .._core import GitHubClient, validate_config, validate_secret

ADAPTER_VERSION = "0.1.0"
CONNECTOR_ID = "github.repositories"
CURSOR_VERSION = "1"
RESOURCE_TYPES = ("repository", "commit", "pull_request", "issue")


class GitHubRepositories:
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
    ) -> None:
        self.config = validate_config(config)
        self.context = context
        self.client = GitHubClient(self.config, validate_secret(secret), context, transport=transport, resolve_dns=resolve_dns)

    # ------------------------------------------------------------ ConnectionCheck

    async def check_connection(self) -> CheckResult:
        write_capable: list[str] = []
        ids: dict[str, int] = {}
        for full in self.config.repositories:
            owner, name = self.client.require_repo(full)
            data, _ = await self.client.get_json(f"/repos/{owner}/{name}")
            ids[full] = int(data["id"])
            perms = data.get("permissions") or {}
            if perms.get("push") or perms.get("admin") or perms.get("maintain"):
                write_capable.append(full)
        rl = self.client.http.last_rate_limit
        remaining, limit = rl.get("x-ratelimit-remaining", "?"), rl.get("x-ratelimit-limit", "?")
        detail = f"read access confirmed for {len(ids)} repositories; rate limit {remaining}/{limit}"
        if write_capable:
            detail += f"; credential is write-capable on {write_capable} (least privilege violated; adapter stays read-only)"
            if self.config.require_read_only_credential:
                return CheckResult(
                    ok=False,
                    identity=self.config.auth_mode,
                    edition=f"REST {self.client.config.api_base}",
                    detail=detail,
                    checked_at=datetime.now(UTC),
                )
        return CheckResult(
            ok=True,
            identity=self.config.auth_mode,
            edition=f"REST {self.client.config.api_base}",
            detail=detail,
            checked_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------ ResourceDiscovery

    async def discover(self, resource_type: str | None = None) -> AsyncIterator[Resource]:
        if resource_type not in (None, "repository"):
            raise UnsupportedOperationError("discover supports resource_type=repository only")
        for full in self.config.repositories:
            owner, name = self.client.require_repo(full)
            data, _ = await self.client.get_json(f"/repos/{owner}/{name}")
            yield Resource(
                resource_type="repository",
                resource_id=str(data["id"]),
                name=data["full_name"],
                parent_id=owner,
                extra={"default_branch": data.get("default_branch"), "private": data.get("private"), "archived": data.get("archived")},
            )

    # ------------------------------------------------------------ RecordSync

    async def sync_page(self, request: SyncRequest) -> SyncPage:
        if request.resource_type not in RESOURCE_TYPES:
            raise UnsupportedOperationError("unsupported resource type", details={"supported": list(RESOURCE_TYPES)})
        owner, name = self.client.require_repo(request.resource_id)
        cursor = _parse_cursor(request.cursor)
        observed = datetime.now(UTC)
        repo, _ = await self.client.get_json(f"/repos/{owner}/{name}")
        repo_id = str(repo["id"])
        if request.resource_type == "repository":
            rec = map_repository(repo, observed.isoformat())
            return SyncPage(
                records=[
                    self._envelope(
                        "repository",
                        rec.source_object_id,
                        rec.source_version,
                        rec.model_dump(mode="json"),
                        repo,
                        f"{self.client.url('/repos/' + owner + '/' + name)}",
                        observed,
                    )
                ],
                next_cursor={"v": 1, "next_url": None, "since": observed.isoformat()},
                complete=True,
                source_counts={"repository": 1},
            )

        since = cursor.get("since")
        since_dt = datetime.fromisoformat(since) - timedelta(seconds=request.overlap_seconds) if since else None
        path, params = self._listing(request.resource_type, repo, since_dt, request.page_size)
        records: list[SourceEnvelope] = []
        failures: list[dict[str, Any]] = []
        max_seen: datetime | None = _dt(cursor.get("max_seen")) or (datetime.fromisoformat(since) if since else None)
        next_url: str | None = None
        async for items, nxt in self.client.paginate(path, params, start_url=cursor.get("next_url"), max_pages=1):
            next_url = nxt
            for raw in items:
                try:
                    env, stamp = self._map_item(request.resource_type, raw, repo_id, owner, name, observed)
                except DataError as exc:
                    failures.append({"id": raw.get("id"), "error": exc.to_dict()})
                    continue
                if since_dt and stamp and stamp < since_dt:
                    continue
                records.append(env)
                if stamp and (max_seen is None or stamp > max_seen):
                    max_seen = stamp
        complete = next_url is None
        new_cursor = {
            "v": 1,
            "next_url": next_url,
            "since": (max_seen.isoformat() if complete and max_seen else since),
            "max_seen": (None if complete else (max_seen.isoformat() if max_seen else None)),
        }
        return SyncPage(
            records=records,
            next_cursor=new_cursor,
            complete=complete,
            partial_failures=failures,
            source_counts={request.resource_type: len(records)},
        )

    def _listing(self, rtype: str, repo: dict[str, Any], since: datetime | None, page_size: int) -> tuple[str, dict[str, Any]]:
        full = repo["full_name"]
        if rtype == "commit":
            params: dict[str, Any] = {"sha": repo.get("default_branch") or "HEAD", "per_page": page_size}
            if since:
                params["since"] = since.isoformat().replace("+00:00", "Z")
            return f"/repos/{full}/commits", params
        if rtype == "pull_request":
            return f"/repos/{full}/pulls", {"state": "all", "sort": "updated", "direction": "asc", "per_page": page_size}
        params = {"state": "all", "sort": "updated", "direction": "asc", "per_page": page_size}
        if since:
            params["since"] = since.isoformat().replace("+00:00", "Z")
        return f"/repos/{full}/issues", params

    def _map_item(
        self, rtype: str, raw: dict[str, Any], repo_id: str, owner: str, name: str, observed: datetime
    ) -> tuple[SourceEnvelope, datetime | None]:
        iso = observed.isoformat()
        if rtype == "commit":
            rec = map_commit(raw, repo_id, iso)
            stamp = _dt(((raw.get("commit") or {}).get("committer") or {}).get("date"))
            prov = raw.get("url") or f"{self.client.url('/repos/' + owner + '/' + name + '/commits/' + rec.sha)}"
            return self._envelope(
                "commit", rec.source_object_id, rec.source_version, rec.model_dump(mode="json"), raw, prov, observed
            ), stamp
        if rtype == "pull_request":
            rec_pr = map_pull_request(raw, repo_id, iso)
            return (
                self._envelope(
                    "pull_request",
                    rec_pr.source_object_id,
                    rec_pr.source_version,
                    rec_pr.model_dump(mode="json"),
                    raw,
                    raw.get("url") or "",
                    observed,
                ),
                _dt(raw.get("updated_at")),
            )
        rec_wi = map_issue(raw, repo_id, iso)
        return (
            self._envelope(
                "issue", rec_wi.source_object_id, rec_wi.source_version, rec_wi.model_dump(mode="json"), raw, raw.get("url") or "", observed
            ),
            _dt(raw.get("updated_at")),
        )

    def _envelope(
        self,
        rtype: str,
        oid: str,
        version: str | None,
        payload: dict[str, Any],
        raw: dict[str, Any],
        provenance: str,
        observed: datetime,
        *,
        event_id: str | None = None,
        operation: Operation = Operation.UPSERT,
    ) -> SourceEnvelope:
        if self.context is None:
            raise PolicyError("adapter is not bound to a tenant/connection context")
        raw_clean = {k: v for k, v in raw.items() if not k.startswith("_")}
        return SourceEnvelope(
            tenant_id=self.context.tenant_id,
            product=self.context.product.value,
            connection_id=self.context.connection_id,
            source_system=CONNECTOR_ID,
            source_edition=f"REST {self.client.config.api_base} {self.client.http._client.headers.get('X-GitHub-Api-Version')}",
            source_object_id=oid,
            source_object_type=f"engineering.{rtype}",
            schema_version="engineering/1",
            mapping_version=MAPPING_VERSION,
            source_version=version,
            event_id=event_id,
            operation=operation,
            source_timestamp=_dt(payload.get("updated_at_iso") or payload.get("authored_at_iso")),
            observed_at=observed,
            checksum=canonical_checksum(payload),
            provenance=provenance,
            payload=payload,
            extensions={"github": raw_clean},
        )

    # ------------------------------------------------------------ WebhookReceiver

    def verify_delivery(self, delivery: WebhookDelivery, secret: bytes) -> WebhookVerdict:
        headers = {k.lower(): v for k, v in delivery.headers.items()}
        sig = headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret, delivery.body, hashlib.sha256).hexdigest()
        if not sig or not hmac.compare_digest(expected, sig):
            return WebhookVerdict(verified=False, delivery_id=None, event_type=None, reason="X-Hub-Signature-256 mismatch")
        return WebhookVerdict(verified=True, delivery_id=headers.get("x-github-delivery"), event_type=headers.get("x-github-event"))

    def envelopes_from_delivery(self, delivery: WebhookDelivery) -> list[SourceEnvelope]:
        headers = {k.lower(): v for k, v in delivery.headers.items()}
        event = headers.get("x-github-event")
        delivery_id = headers.get("x-github-delivery")
        try:
            body = json.loads(delivery.body)
        except ValueError as exc:
            raise ValidationError("webhook body is not JSON") from exc
        repo = body.get("repository") or {}
        full = repo.get("full_name")
        self.client.require_repo(full)  # events for repositories outside the allowlist are rejected
        repo_id = str(repo.get("id"))
        observed = delivery.received_at
        iso = observed.isoformat()
        if event == "pull_request" and body.get("pull_request"):
            rec = map_pull_request(body["pull_request"], repo_id, iso)
            return [
                self._envelope(
                    "pull_request",
                    rec.source_object_id,
                    rec.source_version,
                    rec.model_dump(mode="json"),
                    body["pull_request"],
                    body["pull_request"].get("url") or "",
                    observed,
                    event_id=f"{delivery_id}",
                )
            ]
        if event == "issues" and body.get("issue"):
            if "pull_request" in body["issue"]:
                return []
            rec_wi = map_issue(body["issue"], repo_id, iso)
            op = Operation.DELETE if body.get("action") == "deleted" else Operation.UPSERT
            return [
                self._envelope(
                    "issue",
                    rec_wi.source_object_id,
                    rec_wi.source_version,
                    rec_wi.model_dump(mode="json"),
                    body["issue"],
                    body["issue"].get("url") or "",
                    observed,
                    event_id=f"{delivery_id}",
                    operation=op,
                )
            ]
        if event == "push":
            out = []
            for i, c in enumerate(body.get("commits") or []):
                payload = {
                    "source_system": "github",
                    "source_object_id": c.get("id"),
                    "source_version": c.get("id"),
                    "observed_at_iso": iso,
                    "repository_id": repo_id,
                    "sha": c.get("id"),
                    "author": (c.get("author") or {}).get("name"),
                    "authored_at_iso": c.get("timestamp"),
                    "message": c.get("message"),
                    "parents": [],
                }
                out.append(
                    self._envelope(
                        "commit",
                        str(c.get("id")),
                        str(c.get("id")),
                        payload,
                        c,
                        c.get("url") or "",
                        observed,
                        event_id=f"{delivery_id}#{i}",
                    )
                )
            return out
        return []  # ping and unhandled events are acknowledged but produce no records


def _parse_cursor(cursor: dict[str, Any] | None) -> dict[str, Any]:
    if not cursor:
        return {}
    if cursor.get("v") != 1:
        raise ValidationError("unsupported cursor version", details={"cursor_version": cursor.get("v")})
    return cursor


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


ADAPTER = GitHubRepositories
