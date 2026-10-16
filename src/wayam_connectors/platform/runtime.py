"""Composition root: builds the services from settings once per process."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from wayam_connectors.contracts.capabilities import Capability
from wayam_connectors.contracts.errors import ValidationError

from .config import Settings, get_settings
from .connections import ConnectionService
from .credentials import CredentialStore, backend_from_settings
from .db import Store
from .file_import import FileImportService
from .jobs import JobQueue
from .raw_store import LocalEncryptedRawStore
from .records import RecordStore
from .registry import ConnectorEntry, load_registry
from .tenancy import GrantService, TenantService
from .webhooks import WebhookInbox


@dataclass
class Runtime:
    settings: Settings
    store: Store
    registry: dict[str, ConnectorEntry]
    tenants: TenantService
    grants: GrantService
    connections: ConnectionService
    credentials: CredentialStore
    records: RecordStore
    jobs: JobQueue
    webhooks: WebhookInbox
    file_import: FileImportService

    def adapter_for(self, entry: ConnectorEntry, config: dict[str, Any] | None, secret: dict[str, Any] | None) -> Any:
        """Instantiate an adapter. Parsers take no connection; provider adapters take config + secret."""
        if entry.manifest.capabilities == [Capability.FILE_PARSER]:
            return entry.adapter_cls()
        try:
            return entry.adapter_cls(config or {}, secret or {})
        except PydanticValidationError as exc:
            # never echo input values: the secret part may be among them
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors(include_input=False, include_url=False)
            ]
            raise ValidationError("connection config/secret rejected", details={"errors": errors}) from exc


def build_runtime(settings: Settings | None = None, *, dsn: str | None = None, raw_root: Path | None = None) -> Runtime:
    settings = settings or get_settings()
    store = Store(dsn or settings.database_url)
    backend = backend_from_settings(settings.secret_backend, settings.local_secret_key, settings.azure_key_vault_url)
    registry = load_registry()
    grants = GrantService(store)
    records = RecordStore(store)
    raw = LocalEncryptedRawStore(raw_root or Path(".wayam-raw"), backend)
    return Runtime(
        settings=settings,
        store=store,
        registry=registry,
        tenants=TenantService(store),
        grants=grants,
        connections=ConnectionService(store),
        credentials=CredentialStore(store, backend),
        records=records,
        jobs=JobQueue(store, lease_seconds=settings.job_lease_seconds),
        webhooks=WebhookInbox(store),
        file_import=FileImportService(registry, grants, records, raw),
    )


@lru_cache
def default_runtime() -> Runtime:
    return build_runtime()
