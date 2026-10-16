"""Tenant, product and job identity.

Tenant identity is always derived from validated authentication (API key or
signed job grant). It is never read from a request body.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Product(StrEnum):
    SAMYAK = "samyak"
    BODHAM = "bodham"
    CHAKRAM = "chakram"
    VEDHA = "vedha"
    ANVAYA = "anvaya"
    SANKALP = "sankalp"
    PLATFORM = "platform"  # tenant administration, not a data consumer


class TenantContext(BaseModel):
    """Authenticated caller context. Immutable once minted by the auth layer."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    product: Product
    principal_id: str = Field(description="API key id, user id or worker id that authenticated")
    roles: frozenset[str] = Field(default_factory=frozenset)


class JobGrant(BaseModel):
    """Scoped grant a worker receives for one job: tenant, connection, capabilities, expiry."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    product: Product
    connection_id: UUID
    job_id: UUID
    capabilities: frozenset[str]
    expires_at: datetime


class AdapterContext(BaseModel):
    """Who an adapter instance is working for. Set by the runtime, never by the adapter itself."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    connection_id: UUID
    product: Product
