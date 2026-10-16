"""Opt-in live S3 verification. BLOCKED (skipped with reason) unless WAYAM_LIVE_S3=1 and credentials are present.

Env: WAYAM_LIVE_S3_BUCKET, WAYAM_LIVE_S3_REGION, WAYAM_LIVE_S3_PREFIX (allowlisted prefix with at least one object),
WAYAM_LIVE_S3_AUTH_MODE (access_key|ambient|assume_role), AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY for access_key mode,
WAYAM_LIVE_S3_ROLE_ARN for assume_role. Read-only: the adapter has no write operations.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from wayam_connectors.connectors.aws.s3.adapter import S3Objects
from wayam_connectors.contracts.capabilities import SyncRequest
from wayam_connectors.contracts.errors import PolicyError
from wayam_connectors.contracts.identity import AdapterContext, Product

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def adapter():
    if os.environ.get("WAYAM_LIVE_S3") != "1":
        pytest.skip("BLOCKED: WAYAM_LIVE_S3=1 not set; live S3 verification not run")
    bucket, prefix = os.environ.get("WAYAM_LIVE_S3_BUCKET"), os.environ.get("WAYAM_LIVE_S3_PREFIX", "")
    if not bucket:
        pytest.skip("BLOCKED: WAYAM_LIVE_S3_BUCKET missing")
    mode = os.environ.get("WAYAM_LIVE_S3_AUTH_MODE", "access_key")
    cfg = {
        "bucket": bucket,
        "region": os.environ.get("WAYAM_LIVE_S3_REGION", "us-east-1"),
        "allowed_prefixes": [prefix],
        "auth_mode": mode,
        "role_arn": os.environ.get("WAYAM_LIVE_S3_ROLE_ARN"),
        "use_versions": os.environ.get("WAYAM_LIVE_S3_VERSIONS") == "1",
    }
    secret = (
        {
            "access_key_id": os.environ.get("AWS_ACCESS_KEY_ID"),
            "secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY"),
            "session_token": os.environ.get("AWS_SESSION_TOKEN"),
        }
        if mode == "access_key"
        else {}
    )
    if mode == "access_key" and not secret["access_key_id"]:
        pytest.skip("BLOCKED: AWS_ACCESS_KEY_ID missing for access_key mode")
    return S3Objects(cfg, secret, AdapterContext(tenant_id=uuid4(), connection_id=uuid4(), product=Product.BODHAM)), prefix


async def test_live_s3_list_and_fetch(adapter):
    a, prefix = adapter
    chk = await a.check_connection()
    assert chk.ok, chk.detail
    page = await a.sync_page(SyncRequest(resource_type="object", resource_id=prefix, page_size=5))
    assert page.records, "prefix has no objects; cannot verify listing"
    key = page.records[0].source_object_id
    doc = await a.fetch_document(key)
    assert doc.size > 0
    with pytest.raises(PolicyError):
        await a.fetch_document("definitely-not-allowed/" + key)
