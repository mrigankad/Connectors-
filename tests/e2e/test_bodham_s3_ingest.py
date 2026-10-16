"""Bodham journey from a cloud source: S3 listing -> document ingest (fetch + parse) -> ACL-filtered cited fragments.

aws.s3 is bound to botocore Stubber responses serving the Office fixtures (fixture-level evidence, not AWS).
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from tests.conftest import FIXTURES, TEST_DSN, TEST_KEY, make_tenant
from wayam_connectors.connectors.aws.s3.adapter import S3Objects
from wayam_connectors.contracts.identity import AdapterContext, Product
from wayam_connectors.platform.config import Settings
from wayam_connectors.platform.registry import ConnectorEntry
from wayam_connectors.platform.runtime import build_runtime
from wayam_connectors.worker.main import DEFAULT_HANDLERS, run_once

pytestmark = [pytest.mark.integration, pytest.mark.e2e]
T1 = datetime(2026, 10, 1, tzinfo=UTC)
OFFICE = FIXTURES / "office"


class StubWorld:
    """What the fake bucket contains; the stubbed S3 subclass answers from this."""

    objects: dict[str, tuple[bytes, str]] = {}  # key -> (bytes, etag)


class StubS3(S3Objects):
    def __init__(self, config: dict[str, Any], secret: dict[str, Any], context: AdapterContext | None = None) -> None:
        client = boto3.client(
            "s3", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="y", config=Config(retries={"max_attempts": 0})
        )
        super().__init__(config, secret, context, client=client)
        self.stub = Stubber(client)
        self.stub.activate()

    def _call(self, op: str, **params: Any) -> Any:  # arm the stub just-in-time from the world state
        if op == "list_objects_v2":
            contents = [
                {"Key": k, "ETag": f'"{etag}"', "Size": len(b), "LastModified": T1}
                for k, (b, etag) in sorted(StubWorld.objects.items())
                if k.startswith(params["Prefix"])
            ]
            self.stub.add_response(op, {"IsTruncated": False, "KeyCount": len(contents), "Contents": contents}, {**params})
        elif op == "head_object":
            b, etag = StubWorld.objects[params["Key"]]
            self.stub.add_response(op, {"ContentLength": len(b), "ETag": f'"{etag}"'}, {**params})
        elif op == "get_object":
            b, etag = StubWorld.objects[params["Key"]]
            self.stub.add_response(op, {"Body": StreamingBody(BytesIO(b), len(b)), "ETag": f'"{etag}"'}, {**params})
        elif op == "head_bucket":
            self.stub.add_response(op, {}, ANY)
        elif op == "get_bucket_versioning":
            self.stub.add_response(op, {}, ANY)
        return super()._call(op, **params)


@pytest.fixture(scope="module")
def rt(store, tmp_path_factory):
    settings = Settings(database_url=TEST_DSN, local_secret_key=TEST_KEY, secret_backend="local")
    r = build_runtime(settings, dsn=TEST_DSN, raw_root=tmp_path_factory.mktemp("raw"))
    s3 = r.registry["aws.s3"]
    r.registry["aws.s3"] = ConnectorEntry(manifest=s3.manifest, adapter_cls=StubS3, package=s3.package)
    return r


def _run(rt, t, limit=10):
    out = []
    for _ in range(limit):
        s = run_once(rt, DEFAULT_HANDLERS, "w", tenant_id=t.tenant_id)
        if s is None:
            break
        out.append(s)
        if s == "failed":
            failed = [j for j in rt.jobs.list(t.tenant_id) if j["status"] == "failed"]
            raise AssertionError(f"job failed: {rt.jobs.get(t.tenant_id, failed[0]['id'])['last_error']}")
    return out


def test_s3_to_cited_fragments_behind_acls(rt, tenants):
    t = make_tenant(tenants, "bodham-s3", Product.BODHAM)
    cfg = {
        "bucket": "wayam-docs",
        "region": "ap-south-1",
        "allowed_prefixes": ["shared/", "finance/"],
        "_platform": {"knowledge_acl_principals": ["g:all-staff"]},
    }
    cid = rt.connections.create(t.admin, "aws.s3", "docs bucket", cfg)
    rt.credentials.put(t.tenant_id, cid, {"access_key_id": "AKIAEXAMPLE000000000", "secret_access_key": "x"})
    rt.grants.grant(t.admin, cid, Product.BODHAM, ["RecordSync", "DocumentFetch"])
    rt.knowledge.upsert_principals(
        t.tenant_id,
        cid,
        [
            {"principal_id": "u:alice", "kind": "user", "member_of": ["g:all-staff"]},
            {"principal_id": "u:eve", "kind": "user", "member_of": []},
            {"principal_id": "g:all-staff", "kind": "group"},
        ],
    )
    rt.knowledge.map_principal(t.tenant_id, cid, "bodham-user:alice", "u:alice")
    rt.knowledge.map_principal(t.tenant_id, cid, "bodham-user:eve", "u:eve")
    StubWorld.objects = {
        "shared/review.docx": ((OFFICE / "review.docx").read_bytes(), "etag-r1"),
        "shared/deck.pptx": ((OFFICE / "deck.pptx").read_bytes(), "etag-d1"),
        "shared/notes.unknownformat": (b"binary", "etag-u1"),
    }
    # 1. list objects (full crawl), 2. ingest
    rt.jobs.enqueue(
        t.tenant_id,
        cid,
        Product.BODHAM,
        job_type="record_sync",
        resource_type="object",
        resource_id="shared/",
        payload={"full_crawl": True},
    )
    assert _run(rt, t) == ["succeeded"]
    docs = rt.records.current_records(t.tenant_id, cid, "knowledge.document")
    assert {d["source_object_id"] for d in docs} == set(StubWorld.objects)
    jid = rt.jobs.enqueue(t.tenant_id, cid, Product.BODHAM, job_type="document_ingest", resource_type="ingest")
    assert _run(rt, t) == ["succeeded"]
    res = rt.jobs.get(t.tenant_id, jid)["result"]
    assert res["ingested"] == 2 and res["quarantined"] == 1 and res["fragments"] >= 12 and res["deleted"] == 0
    q = rt.records.quarantined(t.tenant_id, cid)
    assert any(x["source_object_id"] == "shared/notes.unknownformat" and x["error"]["class"] == "unsupported" for x in q)
    # alice (all-staff) sees cited fragments from both files; eve sees nothing (fail closed)
    alice = rt.knowledge.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:alice")
    assert {f["document_id"] for f in alice} == {"shared/review.docx", "shared/deck.pptx"}
    churn = next(f for f in alice if f["text"] == "2.1%")
    assert churn["citation"] == "table 0, row 1, column 1" and churn["provenance"].startswith("s3://wayam-docs/shared/review.docx#")
    assert churn["granted_via"] == "g:all-staff" and churn["acl_version"].startswith("connection:")
    assert rt.knowledge.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:eve") == []
    # 3. new version of the deck + review deleted at the source: re-list (complete crawl) and re-ingest
    StubWorld.objects["shared/deck.pptx"] = (StubWorld.objects["shared/deck.pptx"][0], "etag-d2")
    del StubWorld.objects["shared/review.docx"]
    rt.jobs.enqueue(
        t.tenant_id,
        cid,
        Product.BODHAM,
        job_type="record_sync",
        resource_type="object",
        resource_id="shared/",
        payload={"full_crawl": True},
    )
    assert _run(rt, t) == ["succeeded"]
    jid = rt.jobs.enqueue(t.tenant_id, cid, Product.BODHAM, job_type="document_ingest", resource_type="ingest")
    assert _run(rt, t) == ["succeeded"]
    res = rt.jobs.get(t.tenant_id, jid)["result"]
    assert res["deleted"] == 1 and res["ingested"] == 1 and res["unchanged"] == 0
    alice = rt.knowledge.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:alice")
    assert {f["document_id"] for f in alice} == {"shared/deck.pptx"}
    frags = rt.records.current_records(t.tenant_id, cid, "knowledge.fragment")
    assert all(f["payload"]["version_id"] == "etag-d2" for f in frags if f["payload"]["document_id"] == "shared/deck.pptx")
    assert not any(f["payload"]["document_id"] == "shared/review.docx" for f in frags)
    # 4. nothing changed: ingest is a cheap no-op that only refreshes ACL evidence
    jid = rt.jobs.enqueue(t.tenant_id, cid, Product.BODHAM, job_type="document_ingest", resource_type="ingest")
    assert _run(rt, t) == ["succeeded"]
    res = rt.jobs.get(t.tenant_id, jid)["result"]
    assert res["unchanged"] == 1 and res["ingested"] == 0 and res["fragments"] == 0
    # 5. no principals configured -> nothing readable even though fragments exist
    rt.jobs.enqueue(
        t.tenant_id, cid, Product.BODHAM, job_type="document_ingest", resource_type="ingest", payload={"acl_principals": ["nobody:never"]}
    )
    assert _run(rt, t) == ["succeeded"]
    assert rt.knowledge.visible_fragments(t.product_ctx, cid, wayam_principal="bodham-user:alice") == []
