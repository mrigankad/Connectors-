"""File import pipeline: authorized bytes -> parser -> domain mapping -> envelopes -> one atomic commit.

Used by SAMYAK (CSV statements) and Bodham (DOCX/PPTX fragments). The pipeline:
  1. checks the product's grant on the connection (tenant from auth, never the body),
  2. hashes the input and refuses a repeat import unless explicitly allowed,
  3. parses with the declared adapter and options,
  4. maps rows/fragments to a domain schema, quarantining rows that fail,
  5. reconciles control totals (finance) and fails without writing on mismatch,
  6. commits records, quarantine, checkpoint and outbox in one transaction.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.capabilities import Capability, ParseResult
from wayam_connectors.contracts.envelope import Operation, SourceEnvelope, canonical_checksum
from wayam_connectors.contracts.errors import ConnectorError, DataError, UnsupportedOperationError, ValidationError
from wayam_connectors.contracts.identity import TenantContext
from wayam_connectors.mappings.finance.bank_transactions import (
    MAPPING_VERSION as BANK_TX_MAPPING_VERSION,
)
from wayam_connectors.mappings.finance.bank_transactions import (
    BankTransactionMapping,
    ControlTotals,
    map_row,
    reconcile,
)
from wayam_connectors.platform.security.redaction import redact

from .raw_store import LocalEncryptedRawStore
from .records import QuarantineItem, RecordStore
from .registry import ConnectorEntry
from .tenancy import GrantService

DOMAIN_MAPPINGS = {"finance.bank_transaction": BANK_TX_MAPPING_VERSION, "knowledge.fragment": "knowledge.fragment/1"}


class FileImportService:
    def __init__(
        self, registry: dict[str, ConnectorEntry], grants: GrantService, records: RecordStore, raw_store: LocalEncryptedRawStore | None
    ) -> None:
        self.registry = registry
        self.grants = grants
        self.records = records
        self.raw_store = raw_store

    def import_file(
        self,
        ctx: TenantContext,
        connection_id: UUID,
        *,
        connector_id: str,
        data: bytes,
        source_ref: str,
        parse_options: dict[str, Any] | None,
        domain_mapping: str,
        mapping_config: dict[str, Any] | None = None,
        control_totals: dict[str, Any] | None = None,
        allow_duplicate_input: bool = False,
        job_id: UUID | None = None,
    ) -> dict[str, Any]:
        self.grants.require(ctx, connection_id, Capability.FILE_PARSER.value)
        entry = self.registry.get(connector_id)
        if entry is None or not entry.manifest.supports(Capability.FILE_PARSER):
            raise UnsupportedOperationError("connector does not provide FileParser", details={"connector_id": connector_id})
        if domain_mapping not in DOMAIN_MAPPINGS:
            raise ValidationError("unknown domain mapping", details={"domain_mapping": domain_mapping, "known": sorted(DOMAIN_MAPPINGS)})

        sha = hashlib.sha256(data).hexdigest()
        prior = self.records.get_checkpoint(ctx.tenant_id, connection_id, domain_mapping, sha)
        if prior is not None and not allow_duplicate_input:
            return {"status": "duplicate_input", "source_sha256": sha, "first_imported_at": prior["updated_at"].isoformat(), "stored": 0}

        parser = entry.adapter_cls()
        result: ParseResult = parser.parse(data, source_ref=source_ref, options=parse_options)
        raw_ref = None
        if self.raw_store is not None:
            _, key = self.raw_store.put(ctx.tenant_id, connection_id, data)
            raw_ref = self.records.record_raw_object(
                ctx.tenant_id, connection_id, object_key=key, sha256=sha, size=len(data), content_type=None
            )

        observed = datetime.now(UTC)
        envelopes: list[SourceEnvelope] = []
        quarantine: list[QuarantineItem] = [
            QuarantineItem(provenance=i.location, reason=i.code, error={"message": i.message}) for i in result.issues
        ]
        summary: dict[str, Any] = {}
        if domain_mapping == "finance.bank_transaction":
            mapping = BankTransactionMapping.model_validate(mapping_config or {})
            totals = ControlTotals.model_validate(control_totals) if control_totals else None
            mapped = []
            occurrences: dict[str, int] = {}
            for row in result.items:
                try:
                    rec = map_row(row, mapping, source_system=connector_id, observed_at_iso=observed.isoformat(), occurrence=0)
                    if mapping.id_strategy == "derived":
                        n = occurrences.get(rec.transaction_id, 0)
                        if n:
                            rec = map_row(row, mapping, source_system=connector_id, observed_at_iso=observed.isoformat(), occurrence=n)
                        occurrences[rec.transaction_id] = n + 1
                except DataError as exc:
                    quarantine.append(
                        QuarantineItem(
                            provenance=row["_provenance"],
                            reason="mapping",
                            error=redact(exc.to_dict()),
                            raw={k: str(v) for k, v in row.items() if not k.startswith("_")},
                        )
                    )
                    continue
                mapped.append((row, rec))
            summary = reconcile([r for _, r in mapped], totals, quarantined=len(quarantine))
            for row, rec in mapped:
                payload = rec.model_dump(mode="json")
                envelopes.append(
                    SourceEnvelope(
                        tenant_id=ctx.tenant_id,
                        product=ctx.product.value,
                        connection_id=connection_id,
                        source_system=connector_id,
                        source_edition=entry.manifest.edition,
                        source_object_id=rec.source_object_id,
                        source_object_type="finance.bank_transaction",
                        schema_version="finance/1",
                        mapping_version=BANK_TX_MAPPING_VERSION,
                        source_version=sha,
                        event_id=None,
                        operation=Operation.UPSERT,
                        source_timestamp=None,
                        observed_at=observed,
                        checksum=canonical_checksum(payload),
                        provenance=row["_provenance"],
                        raw_object_ref=raw_ref,
                        payload=payload,
                        extensions={"files.csv": {"row": row["_row"], "source_sha256": sha}},
                    )
                )
        elif domain_mapping == "knowledge.fragment":
            document_id = (mapping_config or {}).get("document_id") or sha
            for item in result.items:
                payload = {"document_id": document_id, "version_id": sha, **item}
                envelopes.append(
                    SourceEnvelope(
                        tenant_id=ctx.tenant_id,
                        product=ctx.product.value,
                        connection_id=connection_id,
                        source_system=connector_id,
                        source_edition=entry.manifest.edition,
                        source_object_id=f"{sha}#{item['fragment_index']}",
                        source_object_type="knowledge.fragment",
                        schema_version="knowledge/1",
                        mapping_version=DOMAIN_MAPPINGS[domain_mapping],
                        source_version=sha,
                        operation=Operation.UPSERT,
                        observed_at=observed,
                        checksum=canonical_checksum(payload),
                        provenance=f"{source_ref}#{item['anchor'].get('kind')}:{item['fragment_index']}",
                        raw_object_ref=raw_ref,
                        payload=payload,
                    )
                )
            summary = {"fragments": len(envelopes), "complete": result.complete, "stats": result.stats}

        stats = self.records.commit_page(
            ctx.tenant_id,
            connection_id,
            resource_type=domain_mapping,
            resource_id=sha,
            envelopes=envelopes,
            quarantine=quarantine,
            next_cursor=None,
            cursor_version=None,
            job_id=job_id,
            mark_success=True,
        )
        return {
            "status": "imported",
            "source_sha256": sha,
            "parser": result.parser,
            "parser_version": result.parser_version,
            "mapping_version": DOMAIN_MAPPINGS[domain_mapping],
            "parse_stats": result.stats,
            "summary": summary,
            "stored": stats.inserted,
            "replayed": stats.replayed,
            "quarantined": stats.quarantined,
            "raw_object_ref": raw_ref,
        }


__all__ = ["FileImportService", "ConnectorError"]
