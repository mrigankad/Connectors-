"""S3 object metadata -> knowledge Document. Content is fetched separately (DocumentFetch) and parsed by format adapters."""

from __future__ import annotations

from typing import Any

from wayam_connectors.contracts.domains.knowledge import Document

MAPPING_VERSION = "knowledge.s3/1"


def map_object(bucket: str, obj: dict[str, Any], observed: str, *, deleted: bool = False, version_id: str | None = None) -> Document:
    key = obj["Key"]
    etag = (obj.get("ETag") or "").strip('"') or None
    version = version_id or obj.get("VersionId")
    return Document(
        source_system="aws.s3",
        source_object_id=key,
        source_version=(f"{version}:{etag}" if etag else version) if version and version != "null" else etag,
        observed_at_iso=observed,
        document_id=key,
        title=key.rsplit("/", 1)[-1] or key,
        mime_type=None,
        path=f"s3://{bucket}/{key}",
        size=obj.get("Size"),
        acl_ref=None,
        deleted=deleted,
    )
