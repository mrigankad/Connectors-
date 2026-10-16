"""Idempotent record storage, checkpoints and the crash-safe commit boundary.

`commit_page` writes records, quarantine rows, the checkpoint and outbox events in
ONE transaction. A crash before commit loses nothing durable (the job re-runs the
page); a crash after commit but before the job acknowledges re-runs the page and
every record is a recognised replay. That is at-least-once with idempotent storage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.envelope import Operation, SourceEnvelope
from wayam_connectors.contracts.errors import ConnectorError

from .db import Store


@dataclass
class QuarantineItem:
    provenance: str
    reason: str
    error: dict[str, Any]
    source_object_id: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class CommitStats:
    inserted: int = 0
    replayed: int = 0
    deleted: int = 0
    quarantined: int = 0
    outbox_events: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "replayed": self.replayed,
            "deleted": self.deleted,
            "quarantined": self.quarantined,
            "outbox_events": self.outbox_events,
            **self.details,
        }


class RecordStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def commit_page(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        *,
        resource_type: str,
        resource_id: str | None,
        envelopes: list[SourceEnvelope],
        quarantine: list[QuarantineItem] | None = None,
        next_cursor: dict[str, Any] | None,
        cursor_version: str | None,
        job_id: UUID | None = None,
        outbox_topic: str | None = "records.committed",
        mark_success: bool = False,
        crawl_id: UUID | None = None,
        seen: list[tuple[str, str]] | None = None,
    ) -> CommitStats:
        """`seen` = (object_type, object_id) pairs observed on this page of a full crawl, recorded in the same transaction."""
        stats = CommitStats()
        with self.store.tenant_tx(tenant_id) as cur:
            for otype, oid in seen or []:
                cur.execute(
                    "INSERT INTO crawl_seen (tenant_id, connection_id, crawl_id, source_object_type, source_object_id) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (tenant_id, connection_id, crawl_id, otype, oid),
                )
            for env in envelopes:
                if env.tenant_id != tenant_id or env.connection_id != connection_id:
                    raise ConnectorError("envelope tenant/connection does not match the committing job")
                version_key = env.event_id or env.source_version or env.checksum
                cur.execute(
                    """
                    INSERT INTO normalized_record (
                        tenant_id, connection_id, product, source_system, source_edition, source_object_type,
                        source_object_id, version_key, source_version, event_id, operation, schema_version,
                        mapping_version, checksum, source_timestamp, observed_at, provenance, raw_object_ref,
                        acl_ref, payload, extensions, is_current)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)
                    ON CONFLICT (tenant_id, connection_id, source_object_type, source_object_id, version_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        env.tenant_id,
                        env.connection_id,
                        env.product,
                        env.source_system,
                        env.source_edition,
                        env.source_object_type,
                        env.source_object_id,
                        version_key,
                        env.source_version,
                        env.event_id,
                        env.operation.value,
                        env.schema_version,
                        env.mapping_version,
                        env.checksum,
                        env.source_timestamp,
                        env.observed_at,
                        env.provenance,
                        env.raw_object_ref,
                        env.acl_ref,
                        json.dumps(env.payload, default=str),
                        json.dumps(env.extensions, default=str),
                    ),
                )
                inserted = cur.fetchone()
                if inserted is None:
                    stats.replayed += 1
                    continue
                stats.inserted += 1
                # previous versions of the same object are no longer current
                cur.execute(
                    "UPDATE normalized_record SET is_current=false WHERE tenant_id=%s AND connection_id=%s "
                    "AND source_object_type=%s AND source_object_id=%s AND id<>%s AND is_current",
                    (tenant_id, connection_id, env.source_object_type, env.source_object_id, inserted["id"]),
                )
                if env.operation == Operation.DELETE:
                    stats.deleted += 1
            for q in quarantine or []:
                cur.execute(
                    "INSERT INTO quarantine_record (tenant_id, connection_id, job_id, source_object_id, provenance, reason, error, raw) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        tenant_id,
                        connection_id,
                        job_id,
                        q.source_object_id,
                        q.provenance,
                        q.reason,
                        json.dumps(q.error, default=str),
                        json.dumps(q.raw, default=str) if q.raw is not None else None,
                    ),
                )
                stats.quarantined += 1
            cur.execute(
                """
                INSERT INTO checkpoint (tenant_id, connection_id, resource_type, resource_id, cursor, cursor_version,
                                        last_success_at, records_total, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() END, %s, now())
                ON CONFLICT (tenant_id, connection_id, resource_type, resource_id) DO UPDATE SET
                    cursor = EXCLUDED.cursor, cursor_version = EXCLUDED.cursor_version,
                    last_success_at = COALESCE(EXCLUDED.last_success_at, checkpoint.last_success_at),
                    records_total = checkpoint.records_total + EXCLUDED.records_total, updated_at = now()
                """,
                (
                    tenant_id,
                    connection_id,
                    resource_type,
                    resource_id or "",
                    json.dumps(next_cursor) if next_cursor is not None else None,
                    cursor_version,
                    mark_success,
                    stats.inserted,
                ),
            )
            if outbox_topic and (stats.inserted or stats.deleted or stats.quarantined):
                cur.execute(
                    "INSERT INTO outbox (tenant_id, connection_id, topic, payload) VALUES (%s,%s,%s,%s)",
                    (
                        tenant_id,
                        connection_id,
                        outbox_topic,
                        json.dumps(
                            {
                                "job_id": str(job_id) if job_id else None,
                                "resource_type": resource_type,
                                "resource_id": resource_id,
                                **stats.as_dict(),
                            }
                        ),
                    ),
                )
                stats.outbox_events += 1
        return stats

    def current_versions(
        self, tenant_id: UUID, connection_id: UUID, source_object_type: str, object_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Current (latest stored) version per object id, including delete markers."""
        if not object_ids:
            return {}
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT source_object_id, source_version, event_id, operation, source_timestamp FROM normalized_record "
                "WHERE tenant_id=%s AND connection_id=%s AND source_object_type=%s AND is_current AND source_object_id = ANY(%s)",
                (tenant_id, connection_id, source_object_type, object_ids),
            )
            return {r["source_object_id"]: dict(r) for r in cur.fetchall()}

    def unseen_current(self, tenant_id: UUID, connection_id: UUID, crawl_id: UUID, source_object_type: str) -> list[str]:
        """Objects that are current and not marked seen by the given crawl: candidates for inferred deletion."""
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT r.source_object_id FROM normalized_record r WHERE r.tenant_id=%s AND r.connection_id=%s "
                "AND r.source_object_type=%s AND r.is_current AND r.operation <> 'delete' AND NOT EXISTS ("
                "  SELECT 1 FROM crawl_seen s WHERE s.tenant_id=r.tenant_id AND s.connection_id=r.connection_id AND s.crawl_id=%s "
                "  AND s.source_object_type=r.source_object_type AND s.source_object_id=r.source_object_id) ORDER BY 1",
                (tenant_id, connection_id, source_object_type, crawl_id),
            )
            return [r["source_object_id"] for r in cur.fetchall()]

    def clear_stale_crawls(self, tenant_id: UUID, connection_id: UUID, *, older_than_days: int = 7) -> int:
        """Marks left by abandoned crawls (cancelled/dead-lettered continuations) are swept, never used for deletes."""
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "DELETE FROM crawl_seen WHERE tenant_id=%s AND connection_id=%s AND seen_at < now() - make_interval(days => %s)",
                (tenant_id, connection_id, older_than_days),
            )
            return cur.rowcount

    def clear_crawl(self, tenant_id: UUID, connection_id: UUID, crawl_id: UUID) -> int:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "DELETE FROM crawl_seen WHERE tenant_id=%s AND connection_id=%s AND crawl_id=%s", (tenant_id, connection_id, crawl_id)
            )
            return cur.rowcount

    def get_checkpoint(self, tenant_id: UUID, connection_id: UUID, resource_type: str, resource_id: str | None) -> dict[str, Any] | None:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT cursor, cursor_version, last_success_at, records_total, updated_at FROM checkpoint "
                "WHERE tenant_id=%s AND connection_id=%s AND resource_type=%s AND resource_id=%s",
                (tenant_id, connection_id, resource_type, resource_id or ""),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def current_records(
        self, tenant_id: UUID, connection_id: UUID, source_object_type: str, *, limit: int = 1000, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT id, source_object_id, source_version, event_id, operation, schema_version, mapping_version, checksum, "
                "source_timestamp, observed_at, provenance, raw_object_ref, acl_ref, payload, extensions "
                "FROM normalized_record WHERE tenant_id=%s AND connection_id=%s AND source_object_type=%s AND is_current "
                + ("" if include_deleted else "AND operation <> 'delete' ")
                + "ORDER BY source_object_id LIMIT %s",
                (tenant_id, connection_id, source_object_type, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def quarantined(self, tenant_id: UUID, connection_id: UUID, *, job_id: UUID | None = None) -> list[dict[str, Any]]:
        with self.store.tenant_tx(tenant_id) as cur:
            if job_id is None:
                cur.execute(
                    "SELECT id, job_id, source_object_id, provenance, reason, error, created_at FROM quarantine_record "
                    "WHERE tenant_id=%s AND connection_id=%s ORDER BY created_at",
                    (tenant_id, connection_id),
                )
            else:
                cur.execute(
                    "SELECT id, job_id, source_object_id, provenance, reason, error, created_at FROM quarantine_record "
                    "WHERE tenant_id=%s AND connection_id=%s AND job_id=%s ORDER BY created_at",
                    (tenant_id, connection_id, job_id),
                )
            return [dict(r) for r in cur.fetchall()]

    def record_raw_object(
        self, tenant_id: UUID, connection_id: UUID, *, object_key: str, sha256: str, size: int, content_type: str | None
    ) -> str:
        """Register retained raw evidence. Returns the raw_object_ref string. Same hash in the same connection is one object."""
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "INSERT INTO raw_object_ref (tenant_id, connection_id, object_key, sha256, size, content_type) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, connection_id, sha256) DO UPDATE SET object_key = raw_object_ref.object_key "
                "RETURNING id, object_key",
                (tenant_id, connection_id, object_key, sha256, size, content_type),
            )
            row = cur.fetchone()
        return f"raw://{row['id']}"  # type: ignore[index]

    def pending_outbox(self, tenant_id: UUID, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute(
                "SELECT id, connection_id, topic, payload, created_at FROM outbox "
                "WHERE tenant_id=%s AND published_at IS NULL ORDER BY id LIMIT %s",
                (tenant_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def mark_published(self, tenant_id: UUID, ids: list[int]) -> None:
        if not ids:
            return
        with self.store.tenant_tx(tenant_id) as cur:
            cur.execute("UPDATE outbox SET published_at = now() WHERE tenant_id=%s AND id = ANY(%s)", (tenant_id, ids))
