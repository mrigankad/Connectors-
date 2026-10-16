"""Capability interfaces.

A connector implements only the capabilities its manifest declares. The
registry checks declared vs implemented at load time; the runtime rejects an
undeclared operation with `UnsupportedOperationError`. There is deliberately no
generic `execute(command)` and no default no-op implementation of anything.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .envelope import SourceEnvelope


class Capability(StrEnum):
    CONNECTION_CHECK = "ConnectionCheck"
    RESOURCE_DISCOVERY = "ResourceDiscovery"
    RECORD_SYNC = "RecordSync"
    DOCUMENT_FETCH = "DocumentFetch"
    PERMISSION_SYNC = "PermissionSync"
    BOUNDED_QUERY = "BoundedQuery"
    WEBHOOK_RECEIVER = "WebhookReceiver"
    REVIEWED_ACTION = "ReviewedAction"
    ARTIFACT_EXPORT = "ArtifactExport"
    INFERENCE = "Inference"
    FILE_PARSER = "FileParser"  # format adapters: bytes in, typed fragments/rows out


# ---------------------------------------------------------------- results


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    ok: bool
    identity: str | None = Field(default=None, description="Who the source thinks we are (no secrets)")
    edition: str | None = None
    detail: str = ""
    checked_at: datetime


class Resource(BaseModel):
    """A discoverable source resource (table, library, repository, project...)."""

    model_config = ConfigDict(frozen=True)
    resource_type: str
    resource_id: str
    name: str
    parent_id: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    extra: dict[str, Any] = Field(default_factory=dict)


class SyncRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    resource_type: str
    resource_id: str | None = None
    cursor: dict[str, Any] | None = Field(default=None, description="Opaque per-partition cursor, versioned")
    page_size: int = 100
    overlap_seconds: int = 0


class SyncPage(BaseModel):
    """One page of envelopes plus the cursor that resumes *after* this page."""

    model_config = ConfigDict(frozen=True)
    records: list[SourceEnvelope]
    next_cursor: dict[str, Any] | None
    complete: bool = Field(description="True when the source reported no further pages")
    partial_failures: list[dict[str, Any]] = Field(default_factory=list)
    source_counts: dict[str, int] = Field(default_factory=dict)


class DocumentContent(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: bytes
    content_type: str | None
    size: int
    etag: str | None = None
    version_id: str | None = None
    acl_ref: str | None = None


class QueryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    sql: str
    params: Mapping[str, Any] = Field(default_factory=dict)
    max_rows: int | None = Field(default=None, description="None = the connection's configured cap; larger than the cap is rejected")
    timeout_seconds: float = 10.0
    user_context: str | None = Field(default=None, description="Source-side user for RLS, if supported")


class QueryResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    row_count: int
    elapsed_ms: float


class WebhookDelivery(BaseModel):
    model_config = ConfigDict(frozen=True)
    headers: Mapping[str, str]
    body: bytes
    received_at: datetime


class WebhookVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)
    verified: bool
    delivery_id: str | None
    event_type: str | None
    reason: str = ""


class ActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: str
    target: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    approval_ref: str | None = None


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    accepted: bool
    provider_ref: str | None
    detail: str


class ParseIssue(BaseModel):
    model_config = ConfigDict(frozen=True)
    location: str
    code: str
    message: str


class ParseResult(BaseModel):
    """Output of a format adapter: typed items with deterministic source locations, plus issues."""

    model_config = ConfigDict(frozen=True)
    parser: str
    parser_version: str
    source_sha256: str
    items: list[dict[str, Any]]
    issues: list[ParseIssue] = Field(default_factory=list)
    complete: bool = True
    stats: dict[str, Any] = Field(default_factory=dict)


# -------------------------------------------------------------- protocols


@runtime_checkable
class ConnectionCheck(Protocol):
    async def check_connection(self) -> CheckResult: ...


@runtime_checkable
class ResourceDiscovery(Protocol):
    def discover(self, resource_type: str | None = None) -> AsyncIterator[Resource]: ...


@runtime_checkable
class RecordSync(Protocol):
    async def sync_page(self, request: SyncRequest) -> SyncPage: ...


@runtime_checkable
class DocumentFetch(Protocol):
    async def fetch_document(self, resource_id: str, version_id: str | None = None) -> DocumentContent: ...


@runtime_checkable
class PermissionSync(Protocol):
    async def sync_permissions(self, request: SyncRequest) -> SyncPage: ...


@runtime_checkable
class BoundedQuery(Protocol):
    async def query(self, request: QueryRequest) -> QueryResult: ...


@runtime_checkable
class WebhookReceiver(Protocol):
    def verify_delivery(self, delivery: WebhookDelivery, secret: bytes) -> WebhookVerdict: ...
    def envelopes_from_delivery(self, delivery: WebhookDelivery) -> list[SourceEnvelope]: ...


@runtime_checkable
class ReviewedAction(Protocol):
    async def perform_action(self, request: ActionRequest) -> ActionResult: ...


@runtime_checkable
class ArtifactExport(Protocol):
    async def export_artifact(self, resource_id: str) -> DocumentContent: ...


@runtime_checkable
class Inference(Protocol):
    async def infer(self, request: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class FileParser(Protocol):
    def parse(self, data: bytes, *, source_ref: str, options: Mapping[str, Any] | None = None) -> ParseResult: ...


CAPABILITY_PROTOCOLS: dict[Capability, type] = {
    Capability.CONNECTION_CHECK: ConnectionCheck,
    Capability.RESOURCE_DISCOVERY: ResourceDiscovery,
    Capability.RECORD_SYNC: RecordSync,
    Capability.DOCUMENT_FETCH: DocumentFetch,
    Capability.PERMISSION_SYNC: PermissionSync,
    Capability.BOUNDED_QUERY: BoundedQuery,
    Capability.WEBHOOK_RECEIVER: WebhookReceiver,
    Capability.REVIEWED_ACTION: ReviewedAction,
    Capability.ARTIFACT_EXPORT: ArtifactExport,
    Capability.INFERENCE: Inference,
    Capability.FILE_PARSER: FileParser,
}

CAPABILITY_METHODS: dict[Capability, tuple[str, ...]] = {
    Capability.CONNECTION_CHECK: ("check_connection",),
    Capability.RESOURCE_DISCOVERY: ("discover",),
    Capability.RECORD_SYNC: ("sync_page",),
    Capability.DOCUMENT_FETCH: ("fetch_document",),
    Capability.PERMISSION_SYNC: ("sync_permissions",),
    Capability.BOUNDED_QUERY: ("query",),
    Capability.WEBHOOK_RECEIVER: ("verify_delivery", "envelopes_from_delivery"),
    Capability.REVIEWED_ACTION: ("perform_action",),
    Capability.ARTIFACT_EXPORT: ("export_artifact",),
    Capability.INFERENCE: ("infer",),
    Capability.FILE_PARSER: ("parse",),
}
