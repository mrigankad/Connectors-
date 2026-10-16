"""The common source envelope every synced record travels in."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Operation(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"
    ACL_CHANGE = "acl_change"


class SourceEnvelope(BaseModel):
    """Typed envelope. Provider-specific fields live under `extensions[<namespace>]`
    so nothing from the source is discarded to fit a universal model."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    product: str = Field(description="Product grant under which the record was fetched")
    connection_id: UUID
    source_system: str = Field(description="Connector id, e.g. files.csv, postgresql.bounded_query")
    source_edition: str | None = None
    source_object_id: str
    source_object_type: str
    schema_version: str
    mapping_version: str | None = None
    source_version: str | None = Field(default=None, description="ETag, row version, commit sha...")
    event_id: str | None = None
    operation: Operation
    source_timestamp: datetime | None = Field(default=None, description="When the source says it changed")
    observed_at: datetime = Field(description="When we observed it")
    checksum: str = Field(description="sha256 of the canonical payload")
    provenance: str = Field(description="Where this came from: URL, file+row, query+row...")
    raw_object_ref: str | None = Field(default=None, description="Reference to retained raw evidence")
    acl_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def unique_key(self) -> tuple[str, str, str, str, str]:
        """Uniqueness includes tenant, connection, object and version/event."""
        return (
            str(self.tenant_id),
            str(self.connection_id),
            self.source_object_type,
            self.source_object_id,
            self.event_id or self.source_version or self.checksum,
        )


def canonical_checksum(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(data).hexdigest()
