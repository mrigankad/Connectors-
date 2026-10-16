"""Connector manifest: the versioned description of one provider surface."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .capabilities import Capability

CONNECTOR_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class VerificationLevel(StrEnum):
    CANDIDATE = "candidate"
    CONTRACT_READY = "contract_ready"
    FIXTURE_TESTED = "fixture_tested"
    LOCAL_INTEGRATION_VERIFIED = "local_integration_verified"
    SANDBOX_VERIFIED = "sandbox_verified"
    PILOT_VERIFIED = "pilot_verified"
    SUPPORTED = "supported"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    RETIRED = "retired"


class Family(StrEnum):
    FILES = "files"
    POSTGRESQL = "postgresql"
    SQLSERVER = "sqlserver"
    AWS = "aws"
    MICROSOFT_GRAPH = "microsoft_graph"
    GITHUB = "github"
    ATLASSIAN = "atlassian"
    VERCEL = "vercel"


class DeletionSemantics(StrEnum):
    NONE = "none"  # source never reports deletes
    EVENT = "event"  # source reports deletes as events (delta/webhook)
    FULL_CRAWL_RECONCILE = "full_crawl_reconcile"  # inferred only after a complete authorized crawl
    VERSION_MARKER = "version_marker"  # e.g. S3 delete markers


class AclCoverage(StrEnum):
    NOT_APPLICABLE = "not_applicable"  # bytes-in parsers inherit caller authorization
    NONE = "none"  # source has ACLs but adapter does not read them
    OBJECT = "object"
    CONTAINER = "container"  # bucket/library/repo level only


class RatePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    requests_per_minute: int | None = None
    concurrency: int = 1
    honour_retry_after: bool = True
    max_attempts: int = 5
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 120.0


class OperationSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    capability: Capability
    description: str
    scopes: list[str] = Field(default_factory=list, description="Least required provider scopes/permissions")
    mutates: bool = False


class ConnectorManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_id: str = Field(description="<family>.<surface>, stable forever")
    catalog_id: str = Field(description="WCN-xxx from the research catalog")
    family: Family
    surface: str
    display_name: str
    edition: str = Field(description="Exact provider edition/region/version this adapter targets")
    adapter_version: str
    schema_version: str
    cursor_version: str | None = None
    auth_modes: list[str]
    config_schema: dict[str, Any] = Field(description="JSON schema for non-secret connection config")
    auth_schema: dict[str, Any] = Field(description="JSON schema for the secret part of the connection")
    capabilities: list[Capability]
    operations: list[OperationSpec]
    resource_types: list[str]
    rate_policy: RatePolicy = Field(default_factory=RatePolicy)
    raw_data_support: bool
    deletion_semantics: DeletionSemantics
    acl_coverage: AclCoverage
    owner: str
    limitations: list[str]
    verification_level: VerificationLevel
    docs_contract: str = Field(description="Path to docs/provider-contracts/<file>.md")

    @field_validator("connector_id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not CONNECTOR_ID_RE.match(v):
            raise ValueError(f"connector_id must look like family.surface, got {v!r}")
        return v

    @field_validator("operations")
    @classmethod
    def _ops_match_caps(cls, ops: list[OperationSpec], info: Any) -> list[OperationSpec]:
        caps = set(info.data.get("capabilities") or [])
        for op in ops:
            if op.capability not in caps:
                raise ValueError(f"operation {op.name} uses undeclared capability {op.capability}")
        return ops

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities
