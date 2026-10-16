"""Connector registry: loads every manifest, checks the adapter class matches it, cross-checks the catalog.

An adapter must implement exactly the capabilities its manifest declares: a declared
capability without the methods is an error, and methods of an undeclared capability
are an error too (no hidden escape hatches).
"""

from __future__ import annotations

import importlib
import importlib.resources
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wayam_connectors.contracts.capabilities import CAPABILITY_METHODS
from wayam_connectors.contracts.manifest import ConnectorManifest, VerificationLevel

from .db import Store

CONNECTORS_PACKAGE = "wayam_connectors.connectors"


@dataclass(frozen=True)
class ConnectorEntry:
    manifest: ConnectorManifest
    adapter_cls: type[Any]
    package: str


class RegistryError(RuntimeError):
    pass


def _iter_manifest_packages() -> list[str]:
    root = importlib.resources.files(CONNECTORS_PACKAGE)
    out: list[str] = []
    for family in root.iterdir():
        if not family.is_dir() or family.name.startswith("_"):
            continue
        for surface in family.iterdir():
            if surface.is_dir() and (surface / "manifest.json").is_file():
                out.append(f"{CONNECTORS_PACKAGE}.{family.name}.{surface.name}")
    return sorted(out)


def check_adapter_conformance(manifest: ConnectorManifest, adapter_cls: type[Any]) -> list[str]:
    problems: list[str] = []
    declared = set(manifest.capabilities)
    for cap, methods in CAPABILITY_METHODS.items():
        has = [m for m in methods if callable(getattr(adapter_cls, m, None))]
        if cap in declared and len(has) != len(methods):
            problems.append(f"{manifest.connector_id}: declares {cap.value} but {adapter_cls.__name__} lacks {set(methods) - set(has)}")
        if cap not in declared and has:
            problems.append(f"{manifest.connector_id}: {adapter_cls.__name__} implements {has} for undeclared capability {cap.value}")
    if getattr(adapter_cls, "CONNECTOR_ID", None) != manifest.connector_id:
        problems.append(f"{manifest.connector_id}: adapter CONNECTOR_ID mismatch ({getattr(adapter_cls, 'CONNECTOR_ID', None)!r})")
    if getattr(adapter_cls, "ADAPTER_VERSION", None) != manifest.adapter_version:
        problems.append(f"{manifest.connector_id}: adapter ADAPTER_VERSION mismatch")
    return problems


def load_registry(*, strict: bool = True) -> dict[str, ConnectorEntry]:
    entries: dict[str, ConnectorEntry] = {}
    problems: list[str] = []
    for pkg in _iter_manifest_packages():
        raw = json.loads((importlib.resources.files(pkg) / "manifest.json").read_text(encoding="utf-8"))
        manifest = ConnectorManifest.model_validate(raw)
        module = importlib.import_module(f"{pkg}.adapter")
        adapter_cls = getattr(module, "ADAPTER", None)
        if adapter_cls is None:
            problems.append(f"{pkg}: adapter.py must export ADAPTER")
            continue
        problems.extend(check_adapter_conformance(manifest, adapter_cls))
        if manifest.connector_id in entries:
            problems.append(f"duplicate connector_id {manifest.connector_id}")
        entries[manifest.connector_id] = ConnectorEntry(manifest=manifest, adapter_cls=adapter_cls, package=pkg)
    if problems and strict:
        raise RegistryError("; ".join(problems))
    return entries


def catalog_paths(repo_root: Path | None = None) -> tuple[Path, Path]:
    root = repo_root or Path(__file__).resolve().parents[3]
    return root / "catalog" / "research" / "Wayam_Connector_Catalog.json", root / "catalog" / "registry.v1.json"


def validate_against_catalog(entries: dict[str, ConnectorEntry], repo_root: Path | None = None) -> list[str]:
    """Every manifest must point at a real research catalog id, and the versioned registry must agree."""
    research_path, registry_path = catalog_paths(repo_root)
    research = {r["id"]: r for r in json.loads(research_path.read_text(encoding="utf-8"))["connectors"]}
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    reg_entries = {e["catalog_id"]: e for e in registry["entries"]}
    problems: list[str] = []
    for cid, e in reg_entries.items():
        if cid not in research:
            problems.append(f"registry entry {cid} is not in the research catalog")
        VerificationLevel(e["status"])
    for entry in entries.values():
        m = entry.manifest
        if m.catalog_id not in research:
            problems.append(f"{m.connector_id}: catalog_id {m.catalog_id} not in research catalog")
        reg = reg_entries.get(m.catalog_id)
        if reg is None:
            problems.append(f"{m.connector_id}: no registry entry for {m.catalog_id}")
        else:
            if reg.get("connector_id") != m.connector_id:
                problems.append(f"{m.connector_id}: registry connector_id is {reg.get('connector_id')!r}")
            if reg.get("status") != m.verification_level.value:
                problems.append(f"{m.connector_id}: manifest level {m.verification_level.value} != registry {reg.get('status')}")
    implemented = {e.manifest.catalog_id for e in entries.values()}
    for cid, reg in reg_entries.items():
        if reg.get("connector_id") and cid not in implemented:
            problems.append(f"registry claims {cid} -> {reg['connector_id']} but no manifest exists")
        if reg["status"] not in ("candidate", "contract_ready", "blocked") and cid not in implemented:
            problems.append(f"registry {cid} is {reg['status']} without an implementation")
    return problems


def sync_definitions(store: Store, entries: dict[str, ConnectorEntry]) -> int:
    n = 0
    with store.bypass_tx("admin") as cur:
        for e in entries.values():
            m = e.manifest
            cur.execute(
                "INSERT INTO connector_definition (connector_id, catalog_id, adapter_version, schema_version, "
                "verification_level, manifest, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (connector_id) DO UPDATE SET catalog_id=EXCLUDED.catalog_id, "
                "adapter_version=EXCLUDED.adapter_version, schema_version=EXCLUDED.schema_version, "
                "verification_level=EXCLUDED.verification_level, manifest=EXCLUDED.manifest, updated_at=now()",
                (
                    m.connector_id,
                    m.catalog_id,
                    m.adapter_version,
                    m.schema_version,
                    m.verification_level.value,
                    json.dumps(m.model_dump(mode="json")),
                ),
            )
            n += 1
    return n
