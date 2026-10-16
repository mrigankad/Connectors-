"""aws.s3 against botocore Stubber responses (fixture level: shapes come from the official service model)."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from wayam_connectors.connectors.aws.s3.adapter import S3Objects
from wayam_connectors.contracts.capabilities import SyncRequest
from wayam_connectors.contracts.errors import (
    AuthError,
    PolicyError,
    RateLimitedError,
    ScopeError,
    UnsupportedOperationError,
    ValidationError,
)
from wayam_connectors.contracts.identity import AdapterContext, Product

CTX = AdapterContext(tenant_id=uuid4(), connection_id=uuid4(), product=Product.BODHAM)
CFG = {"bucket": "wayam-test-bucket", "region": "ap-south-1", "allowed_prefixes": ["finance/", "docs/"]}
SECRET = {"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
T1 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def _adapter(**over):
    client = boto3.client(
        "s3", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="y", config=Config(retries={"max_attempts": 0})
    )
    a = S3Objects({**CFG, **over}, SECRET, CTX, client=client)
    return a, Stubber(client)


def test_config_validation_and_prefix_policy():
    with pytest.raises(ValidationError):
        S3Objects({"bucket": "b"}, SECRET, CTX)  # allowed_prefixes missing
    with pytest.raises(ValidationError):
        S3Objects(CFG, {}, CTX)  # access_key mode needs keys
    with pytest.raises(ValidationError):
        S3Objects({**CFG, "endpoint_url": "http://minio.local"}, SECRET, CTX)
    with pytest.raises(PolicyError):
        S3Objects({**CFG, "endpoint_url": "https://10.0.0.5:9000"}, SECRET, CTX)
    assert S3Objects({**CFG, "endpoint_url": "https://10.0.0.5:9000", "allow_private_network": True}, SECRET, CTX)
    a, _ = _adapter()
    with pytest.raises(PolicyError):
        a._require_key("secrets/key.txt")
    with pytest.raises(ValidationError):
        a._require_key("finance/../secrets")
    assert a._require_key("finance/2026/stmt.csv") == "finance/2026/stmt.csv"


async def test_check_connection_and_versioning_requirement():
    a, stub = _adapter()
    stub.add_response("head_bucket", {}, {"Bucket": "wayam-test-bucket"})
    stub.add_response("list_objects_v2", {"KeyCount": 0}, {"Bucket": "wayam-test-bucket", "Prefix": "finance/", "MaxKeys": 1})
    stub.add_response("list_objects_v2", {"KeyCount": 0}, {"Bucket": "wayam-test-bucket", "Prefix": "docs/", "MaxKeys": 1})
    stub.add_response("get_bucket_versioning", {"Status": "Suspended"}, {"Bucket": "wayam-test-bucket"})
    with stub:
        res = await a.check_connection()
    assert res.ok and "versioning=Suspended" in res.detail and res.identity == "access_key"
    a, stub = _adapter(use_versions=True)
    stub.add_response("head_bucket", {}, {"Bucket": "wayam-test-bucket"})
    stub.add_response("list_objects_v2", {"KeyCount": 0}, {"Bucket": ANY, "Prefix": "finance/", "MaxKeys": 1})
    stub.add_response("list_objects_v2", {"KeyCount": 0}, {"Bucket": ANY, "Prefix": "docs/", "MaxKeys": 1})
    stub.add_response("get_bucket_versioning", {}, {"Bucket": ANY})
    with stub:
        res = await a.check_connection()
    assert not res.ok and "needs versioning" in res.detail
    a, stub = _adapter()
    stub.add_client_error("head_bucket", service_error_code="403", service_message="Forbidden", http_status_code=403)
    with stub, pytest.raises(ScopeError):
        await a.check_connection()
    a, stub = _adapter()
    stub.add_client_error("head_bucket", service_error_code="InvalidAccessKeyId", http_status_code=403)
    with stub, pytest.raises(AuthError):
        await a.check_connection()
    found = [r async for r in a.discover()]
    assert [r.resource_id for r in found] == ["finance/", "docs/"]
    with pytest.raises(UnsupportedOperationError):
        _ = [r async for r in a.discover("bucket")]


async def test_listing_pages_cursor_and_truncation_checks():
    a, stub = _adapter()
    page1 = {
        "IsTruncated": True,
        "NextContinuationToken": "tok-2",
        "KeyCount": 2,
        "Contents": [
            {"Key": "finance/a.csv", "ETag": '"e1"', "Size": 10, "LastModified": T1},
            {"Key": "finance/b.csv", "ETag": '"e2"', "Size": 20, "LastModified": T1},
        ],
    }
    page2 = {"IsTruncated": False, "KeyCount": 1, "Contents": [{"Key": "finance/c.csv", "ETag": '"e3"', "Size": 30, "LastModified": T1}]}
    stub.add_response("list_objects_v2", page1, {"Bucket": "wayam-test-bucket", "Prefix": "finance/", "MaxKeys": 2})
    stub.add_response(
        "list_objects_v2", page2, {"Bucket": "wayam-test-bucket", "Prefix": "finance/", "MaxKeys": 2, "ContinuationToken": "tok-2"}
    )
    with stub:
        p1 = await a.sync_page(SyncRequest(resource_type="object", resource_id="finance/", page_size=2))
        p2 = await a.sync_page(SyncRequest(resource_type="object", resource_id="finance/", page_size=2, cursor=p1.next_cursor))
    assert not p1.complete and p1.next_cursor == {"v": 1, "continuation_token": "tok-2"} and p1.source_counts["key_count"] == 2
    assert [e.source_object_id for e in p1.records] == ["finance/a.csv", "finance/b.csv"] and p1.records[0].source_version == "e1"
    assert p1.records[0].payload["path"] == "s3://wayam-test-bucket/finance/a.csv" and p1.records[0].source_timestamp == T1
    assert p1.records[0].extensions["s3"]["LastModified"] == T1.isoformat() and p1.records[0].mapping_version == "knowledge.s3/1"
    assert p2.complete and p2.next_cursor == {"v": 1} and len(p2.records) == 1
    with pytest.raises(PolicyError):
        await a.sync_page(SyncRequest(resource_type="object", resource_id="secrets/"))
    with pytest.raises(UnsupportedOperationError):
        await a.sync_page(SyncRequest(resource_type="bucket", resource_id="finance/"))
    a, stub = _adapter()
    stub.add_response(
        "list_objects_v2", {"IsTruncated": True, "Contents": []}, {"Bucket": ANY, "Prefix": ANY, "MaxKeys": ANY}
    )  # truncated, no token
    with stub, pytest.raises(ValidationError, match="NextContinuationToken"):
        await a.sync_page(SyncRequest(resource_type="object", resource_id="finance/"))
    a, stub = _adapter()
    stub.add_response(
        "list_objects_v2",
        {"IsTruncated": True, "NextContinuationToken": "same", "Contents": []},
        {"Bucket": ANY, "Prefix": ANY, "MaxKeys": ANY, "ContinuationToken": "same"},
    )
    with stub, pytest.raises(ValidationError, match="cycle"):
        await a.sync_page(SyncRequest(resource_type="object", resource_id="finance/", cursor={"v": 1, "continuation_token": "same"}))
    a, stub = _adapter()
    stub.add_client_error("list_objects_v2", service_error_code="SlowDown", http_status_code=503)
    with stub, pytest.raises(RateLimitedError):
        await a.sync_page(SyncRequest(resource_type="object", resource_id="finance/"))


async def test_versions_and_delete_markers():
    a, stub = _adapter(use_versions=True)
    resp = {
        "IsTruncated": True,
        "NextKeyMarker": "docs/b.docx",
        "NextVersionIdMarker": "v9",
        "Versions": [
            {"Key": "docs/a.docx", "VersionId": "v2", "ETag": '"x2"', "Size": 5, "IsLatest": True, "LastModified": T1},
            {"Key": "docs/a.docx", "VersionId": "v1", "ETag": '"x1"', "Size": 4, "IsLatest": False, "LastModified": T1},
        ],
        "DeleteMarkers": [{"Key": "docs/old.docx", "VersionId": "d1", "IsLatest": True, "LastModified": T1}],
    }
    stub.add_response("list_object_versions", resp, {"Bucket": "wayam-test-bucket", "Prefix": "docs/", "MaxKeys": 1000})
    stub.add_response(
        "list_object_versions",
        {"IsTruncated": False, "Versions": []},
        {"Bucket": "wayam-test-bucket", "Prefix": "docs/", "MaxKeys": 1000, "KeyMarker": "docs/b.docx", "VersionIdMarker": "v9"},
    )
    with stub:
        p1 = await a.sync_page(SyncRequest(resource_type="object", resource_id="docs/", page_size=1000))
        p2 = await a.sync_page(SyncRequest(resource_type="object", resource_id="docs/", page_size=1000, cursor=p1.next_cursor))
    ops = [(e.source_object_id, e.source_version, e.operation.value) for e in p1.records]
    assert ops == [("docs/a.docx", "v2:x2", "upsert"), ("docs/a.docx", "v1:x1", "upsert"), ("docs/old.docx", "d1", "delete")]
    assert p1.records[2].payload["deleted"] is True and p1.next_cursor == {"v": 1, "key_marker": "docs/b.docx", "version_id_marker": "v9"}
    assert p2.complete


async def test_fetch_document_size_cap_and_prefix():
    a, stub = _adapter(max_object_bytes=16)
    stub.add_response("head_object", {"ContentLength": 5}, {"Bucket": "wayam-test-bucket", "Key": "docs/a.txt", "VersionId": "v2"})
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(BytesIO(b"hello"), 5), "ContentType": "text/plain", "ETag": '"abc"', "VersionId": "v2"},
        {"Bucket": "wayam-test-bucket", "Key": "docs/a.txt", "VersionId": "v2"},
    )
    with stub:
        doc = await a.fetch_document("docs/a.txt", "v2")
    assert doc.content == b"hello" and doc.etag == "abc" and doc.version_id == "v2" and doc.content_type == "text/plain"
    a, stub = _adapter(max_object_bytes=16)
    stub.add_response("head_object", {"ContentLength": 17}, {"Bucket": ANY, "Key": "docs/big.bin"})
    with stub, pytest.raises(PolicyError, match="size limit"):
        await a.fetch_document("docs/big.bin")
    with pytest.raises(PolicyError):
        await a.fetch_document("private/x.txt")
    a, stub = _adapter()
    stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
    with stub, pytest.raises(ScopeError):
        await a.fetch_document("docs/missing.txt")
