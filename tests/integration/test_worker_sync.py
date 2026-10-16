"""Worker sync engine: page loop, crash/replay, lease loss, cursor resume, full-crawl deletes, webhook ordering.

Uses an in-memory RecordSync/WebhookReceiver adapter registered into the runtime for the test, plus the real
GitHub repositories adapter bound to the captured fixtures (Chakram lineage: commits land as engineering records).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.conftest import TEST_DSN, TEST_KEY, make_tenant
from tests.contract.test_github_fixtures import Fake
from wayam_connectors.connectors.github.repositories.adapter import GitHubRepositories
from wayam_connectors.contracts.capabilities import Capability, SyncPage, SyncRequest, WebhookDelivery, WebhookVerdict
from wayam_connectors.contracts.envelope import Operation, SourceEnvelope, canonical_checksum
from wayam_connectors.contracts.errors import CursorExpiredError, RetryableError
from wayam_connectors.contracts.identity import AdapterContext, Product
from wayam_connectors.contracts.manifest import ConnectorManifest
from wayam_connectors.platform.config import Settings
from wayam_connectors.platform.registry import ConnectorEntry, sync_definitions
from wayam_connectors.platform.runtime import build_runtime
from wayam_connectors.worker.main import DEFAULT_HANDLERS, run_once

pytestmark = pytest.mark.integration


class MemorySource:
    """Deterministic source: 3 pages of 2 objects, optional failure on a page, optional cursor expiry, webhooks."""

    CONNECTOR_ID = "test.memory"
    ADAPTER_VERSION = "0.0.1"
    objects: dict[str, dict[str, Any]] = {}
    fail_on_page: int | None = None
    expire_cursor_once = False

    def __init__(self, config: dict[str, Any], secret: dict[str, Any], context: AdapterContext | None = None) -> None:
        self.ctx = context

    def _env(
        self, oid: str, version: str, op: Operation = Operation.UPSERT, event_id: str | None = None, ts: datetime | None = None
    ) -> SourceEnvelope:
        assert self.ctx is not None
        payload = {"id": oid, "v": version}
        return SourceEnvelope(
            tenant_id=self.ctx.tenant_id,
            product=self.ctx.product.value,
            connection_id=self.ctx.connection_id,
            source_system="test.memory",
            source_object_id=oid,
            source_object_type="test.object",
            schema_version="1",
            source_version=version,
            event_id=event_id,
            operation=op,
            source_timestamp=ts,
            observed_at=datetime.now(UTC),
            checksum=canonical_checksum(payload),
            provenance=f"mem://{oid}",
            payload=payload,
        )

    async def sync_page(self, request: SyncRequest) -> SyncPage:
        page = (request.cursor or {}).get("page", 0)
        if MemorySource.expire_cursor_once and page > 0:
            MemorySource.expire_cursor_once = False
            raise CursorExpiredError("delta token expired")
        if MemorySource.fail_on_page == page:
            MemorySource.fail_on_page = None
            raise RetryableError("source hiccup")
        ids = sorted(MemorySource.objects)
        chunk = ids[page * 2 : page * 2 + 2]
        records = [self._env(oid, MemorySource.objects[oid]["v"]) for oid in chunk]
        complete = (page + 1) * 2 >= len(ids)
        return SyncPage(
            records=records,
            next_cursor={"v": 1, "page": page + 1} if not complete else {"v": 1, "page": 0, "done": True},
            complete=complete,
            partial_failures=[{"id": "broken", "error": {"message": "unmappable"}}] if page == 0 else [],
        )

    def verify_delivery(self, delivery: WebhookDelivery, secret: bytes) -> WebhookVerdict:
        ok = hmac.compare_digest(hmac.new(secret, delivery.body, hashlib.sha256).hexdigest(), delivery.headers.get("x-sig", ""))
        return WebhookVerdict(verified=ok, delivery_id=delivery.headers.get("x-id"), event_type="object")

    def envelopes_from_delivery(self, delivery: WebhookDelivery) -> list[SourceEnvelope]:
        b = json.loads(delivery.body)
        ts = datetime.fromisoformat(b["ts"])
        return [
            self._env(
                b["id"], b["v"], Operation.DELETE if b.get("deleted") else Operation.UPSERT, event_id=delivery.headers.get("x-id"), ts=ts
            )
        ]


def _manifest(connector_id: str, deletion: str) -> ConnectorManifest:
    return ConnectorManifest(
        connector_id=connector_id,
        catalog_id="WCN-283",
        family="files",
        surface=connector_id.split(".")[1],
        display_name="test",
        edition="test",
        adapter_version="0.0.1",
        schema_version="1",
        cursor_version="1",
        auth_modes=["inherited"],
        config_schema={},
        auth_schema={},
        capabilities=[Capability.RECORD_SYNC, Capability.WEBHOOK_RECEIVER],
        operations=[],
        resource_types=["object"],
        raw_data_support=False,
        deletion_semantics=deletion,
        acl_coverage="none",
        owner="test",
        limitations=[],
        verification_level="candidate",
        docs_contract="none",
    )


class FixtureGitHub(GitHubRepositories):
    """The real adapter bound to the captured fixtures through a mock transport."""

    def __init__(self, config: dict[str, Any], secret: dict[str, Any], context: AdapterContext | None = None) -> None:
        super().__init__(config, secret, context, transport=httpx.MockTransport(Fake()), resolve_dns=False)


@pytest.fixture(scope="module")
def rt(store, tmp_path_factory):
    settings = Settings(database_url=TEST_DSN, local_secret_key=TEST_KEY, secret_backend="local")
    r = build_runtime(settings, dsn=TEST_DSN, raw_root=tmp_path_factory.mktemp("raw"))
    r.registry["test.memory"] = ConnectorEntry(
        manifest=_manifest("test.memory", "full_crawl_reconcile"), adapter_cls=MemorySource, package="tests"
    )
    r.registry["test.events"] = ConnectorEntry(manifest=_manifest("test.events", "event"), adapter_cls=MemorySource, package="tests")
    gh = r.registry["github.repositories"]
    r.registry["github.repositories"] = ConnectorEntry(manifest=gh.manifest, adapter_cls=FixtureGitHub, package=gh.package)
    sync_definitions(r.store, r.registry)
    return r


def _conn(rt, t, connector: str, product=Product.CHAKRAM, config=None, secret=None, caps=("RecordSync", "WebhookReceiver")):
    cid = rt.connections.create(t.admin, connector, "c", config or {})
    if secret:
        rt.credentials.put(t.tenant_id, cid, secret)
    rt.grants.grant(t.admin, cid, product, list(caps))
    return cid


def _run_all(rt, t, worker="w", limit=10):
    out = []
    for _ in range(limit):
        s = run_once(rt, DEFAULT_HANDLERS, worker, tenant_id=t.tenant_id)
        if s is None:
            break
        out.append(s)
    return out


def test_sync_pages_commit_resume_retry_and_full_crawl_deletes(rt, tenants):
    t = make_tenant(tenants, "sync-tenant", Product.CHAKRAM)
    cid = _conn(rt, t, "test.memory")
    MemorySource.objects = {f"o{i}": {"v": "v1"} for i in range(1, 6)}  # 5 objects -> 3 pages
    # page budget of 1 per job: each job commits one page and re-enqueues itself (resume), checkpoint carries the cursor
    jid = rt.jobs.enqueue(
        t.tenant_id, cid, Product.CHAKRAM, job_type="record_sync", resource_type="object", payload={"max_pages": 1, "page_size": 2}
    )
    assert run_once(rt, DEFAULT_HANDLERS, "w", tenant_id=t.tenant_id) == "resumed"
    assert rt.jobs.get(t.tenant_id, jid)["result"]["pages"] == 1
    assert rt.records.get_checkpoint(t.tenant_id, cid, "object", None)["cursor"] == {"v": 1, "page": 1}
    assert len(rt.records.current_records(t.tenant_id, cid, "test.object")) == 2
    assert [q["reason"] for q in rt.records.quarantined(t.tenant_id, cid)] == ["partial_failure"]
    # T-16/T-21: the source fails on page 2 -> job queued for retry, checkpoint untouched, nothing half-written
    MemorySource.fail_on_page = 1
    statuses = _run_all(rt, t)
    assert statuses[0] == "queued"
    assert rt.records.get_checkpoint(t.tenant_id, cid, "object", None)["cursor"] == {"v": 1, "page": 1}
    # retry runs after backoff: wait it out by forcing run_after
    with rt.store.tenant_tx(t.tenant_id) as cur:
        cur.execute("UPDATE sync_job SET run_after = now() WHERE tenant_id=%s AND status='queued'", (t.tenant_id,))
    assert _run_all(rt, t)[-1] == "succeeded"
    assert len(rt.records.current_records(t.tenant_id, cid, "test.object")) == 5
    cp = rt.records.get_checkpoint(t.tenant_id, cid, "object", None)
    assert cp["last_success_at"] is not None and cp["cursor"]["done"] is True
    # replay the same data: all replays, nothing duplicated (T-13)
    jid2 = rt.jobs.enqueue(
        t.tenant_id, cid, Product.CHAKRAM, job_type="record_sync", resource_type="object", payload={"max_pages": 10, "full_crawl": True}
    )
    assert _run_all(rt, t) == ["succeeded"]
    res = rt.jobs.get(t.tenant_id, jid2)["result"]
    assert res["complete"] and res["replayed"] == 5 and res["inserted"] == 0 and res["inferred_deletes"] == 0
    # T-14/T-15: object o3 disappears at the source. A partial crawl must not delete it; a complete one must.
    del MemorySource.objects["o3"]
    MemorySource.objects["o1"]["v"] = "v2"
    rt.jobs.enqueue(
        t.tenant_id, cid, Product.CHAKRAM, job_type="record_sync", resource_type="object", payload={"max_pages": 1, "full_crawl": True}
    )
    assert run_once(rt, DEFAULT_HANDLERS, "w", tenant_id=t.tenant_id) == "resumed"  # page 1 of 2 only
    assert {r["source_object_id"] for r in rt.records.current_records(t.tenant_id, cid, "test.object")} == {"o1", "o2", "o3", "o4", "o5"}
    # cancel the continuation: crawl stays incomplete, still no deletion inferred
    pending = [j for j in rt.jobs.list(t.tenant_id, cid) if j["status"] == "queued"]
    assert len(pending) == 1 and rt.jobs.cancel(t.tenant_id, pending[0]["id"])
    assert "o3" in {r["source_object_id"] for r in rt.records.current_records(t.tenant_id, cid, "test.object")}
    # a complete full crawl infers the deletion and records the new version of o1
    rt.jobs.enqueue(
        t.tenant_id, cid, Product.CHAKRAM, job_type="record_sync", resource_type="object", payload={"max_pages": 10, "full_crawl": True}
    )
    assert _run_all(rt, t) == ["succeeded"]
    current = {r["source_object_id"]: r["source_version"] for r in rt.records.current_records(t.tenant_id, cid, "test.object")}
    assert current == {"o1": "v2", "o2": "v1", "o4": "v1", "o5": "v1"}
    deleted = [r for r in rt.records.current_records(t.tenant_id, cid, "test.object", include_deleted=True) if r["operation"] == "delete"]
    assert len(deleted) == 1 and deleted[0]["source_object_id"] == "o3" and "full-crawl" in deleted[0]["provenance"]
    done = [j for j in rt.jobs.list(t.tenant_id, cid) if j["status"] == "succeeded"][0]
    crawl_id = rt.jobs.get(t.tenant_id, done["id"])["result"]["crawl_id"]
    with rt.store.tenant_tx(t.tenant_id) as cur:
        cur.execute("SELECT count(*) AS n FROM crawl_seen WHERE connection_id=%s AND crawl_id=%s", (cid, crawl_id))
        assert cur.fetchone()["n"] == 0  # the completed crawl's marks are cleared
        cur.execute("SELECT count(*) AS n FROM crawl_seen WHERE connection_id=%s", (cid,))
        assert cur.fetchone()["n"] == 2  # the cancelled crawl's marks stay until swept (7 days); they never drive deletes
    assert rt.records.clear_stale_crawls(t.tenant_id, cid, older_than_days=0) == 2
    topics = [o["topic"] for o in rt.records.pending_outbox(t.tenant_id)]
    assert "records.deleted" in topics


def test_event_source_never_infers_deletes_and_cursor_expiry_resyncs(rt, tenants):
    t = make_tenant(tenants, "events-tenant", Product.CHAKRAM)
    cid = _conn(rt, t, "test.events")
    MemorySource.objects = {"a": {"v": "1"}, "b": {"v": "1"}, "c": {"v": "1"}}
    rt.jobs.enqueue(t.tenant_id, cid, Product.CHAKRAM, job_type="record_sync", resource_type="object", payload={"full_crawl": True})
    assert _run_all(rt, t) == ["succeeded"]
    del MemorySource.objects["c"]
    jid = rt.jobs.enqueue(t.tenant_id, cid, Product.CHAKRAM, job_type="record_sync", resource_type="object", payload={"full_crawl": True})
    assert _run_all(rt, t) == ["succeeded"]
    res = rt.jobs.get(t.tenant_id, jid)["result"]
    assert res["inferred_deletes"] == 0 and "nothing inferred" in res["note"]
    assert len(rt.records.current_records(t.tenant_id, cid, "test.object")) == 3  # c stays: event sources report deletes explicitly
    # T-12: expired cursor -> quarantine note, retry starts from scratch and completes
    MemorySource.objects["c"] = {"v": "1"}
    MemorySource.expire_cursor_once = True
    jid = rt.jobs.enqueue(t.tenant_id, cid, Product.CHAKRAM, job_type="record_sync", resource_type="object", payload={"max_pages": 1})
    assert run_once(rt, DEFAULT_HANDLERS, "w", tenant_id=t.tenant_id) == "resumed"
    s = run_once(rt, DEFAULT_HANDLERS, "w", tenant_id=t.tenant_id)
    assert s == "failed"  # CursorExpiredError is not retryable by itself: the resync is explicit
    assert any(q["reason"] == "cursor_expired" for q in rt.records.quarantined(t.tenant_id, cid))
    assert rt.records.get_checkpoint(t.tenant_id, cid, "object", None)["cursor"] is None
    failed = [j for j in rt.jobs.list(t.tenant_id, cid) if j["status"] == "failed"]
    assert rt.jobs.replay(t.tenant_id, failed[0]["id"])
    assert _run_all(rt, t)[-1] in ("succeeded", "resumed")


def test_webhook_processing_orders_and_dedups(rt, tenants):
    t = make_tenant(tenants, "hook-tenant", Product.CHAKRAM)
    cid = _conn(rt, t, "test.events")
    secret = b"s"
    base = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

    def deliver(did: str, oid: str, v: str, minutes: int, deleted: bool = False) -> None:
        body = json.dumps({"id": oid, "v": v, "ts": (base + timedelta(minutes=minutes)).isoformat(), "deleted": deleted}).encode()
        sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
        rt.webhooks.receive(t.tenant_id, cid, MemorySource({}, {}, None), headers={"x-sig": sig, "x-id": did}, body=body, secret=secret)

    deliver("d1", "x", "v2", 10)  # newer state first
    deliver("d2", "x", "v1", 5)  # then the older update arrives late (T-26)
    deliver("d3", "y", "v1", 1)
    deliver("d3", "y", "v1", 1)  # exact duplicate delivery id -> inbox dedup (not stored twice)
    deliver("d5", "y", "gone", 7, deleted=True)
    deliver("d6", "y", "v2", 6)  # update older than the delete -> ignored, y stays deleted
    jid = rt.jobs.enqueue(t.tenant_id, cid, Product.CHAKRAM, job_type="webhook_process", resource_type="inbox")
    assert _run_all(rt, t) == ["succeeded"]
    res = rt.jobs.get(t.tenant_id, jid)["result"]
    assert res == {"deliveries": 5, "applied": 3, "stale_ignored": 2, "rejected": 0}
    cur = {
        r["source_object_id"]: (r["source_version"], r["operation"])
        for r in rt.records.current_records(t.tenant_id, cid, "test.object", include_deleted=True)
    }
    assert cur == {"x": ("v2", "upsert"), "y": ("gone", "delete")}
    assert rt.webhooks.pending(t.tenant_id, cid) == []
    # re-running with nothing pending is a no-op
    rt.jobs.enqueue(t.tenant_id, cid, Product.CHAKRAM, job_type="webhook_process", resource_type="inbox")
    assert _run_all(rt, t) == ["succeeded"]


def test_chakram_lineage_commits_and_runs_through_the_engine(rt, tenants):
    """Repository -> commits (github.repositories on captured fixtures) land as engineering records via the worker."""
    t = make_tenant(tenants, "chakram-tenant", Product.CHAKRAM)
    cfg = {"repositories": ["mrigankad/Chakram"], "auth_mode": "token", "require_read_only_credential": False}
    cid = _conn(rt, t, "github.repositories", config=cfg, secret={"token": "ghp_fixture"}, caps=("RecordSync",))
    for rtype in ("repository", "commit", "pull_request"):
        rt.jobs.enqueue(
            t.tenant_id,
            cid,
            Product.CHAKRAM,
            job_type="record_sync",
            resource_type=rtype,
            resource_id="mrigankad/Chakram",
            payload={"page_size": 2},
        )
    statuses = _run_all(rt, t, limit=20)
    assert statuses and all(s == "succeeded" for s in statuses)
    repo = rt.records.current_records(t.tenant_id, cid, "engineering.repository")
    commits = rt.records.current_records(t.tenant_id, cid, "engineering.commit")
    prs = rt.records.current_records(t.tenant_id, cid, "engineering.pull_request")
    assert repo[0]["payload"]["repository_id"] == "1422286600" and len(commits) == 2 and len(prs) == 2
    assert all(c["payload"]["repository_id"] == "1422286600" for c in commits)  # lineage key shared across objects
    assert prs[0]["payload"]["head_sha"] and prs[0]["mapping_version"] == "engineering.github/1"
    cp = rt.records.get_checkpoint(t.tenant_id, cid, "commit", "mrigankad/Chakram")
    assert cp["cursor_version"] == "1" and cp["cursor"]["next_url"] is None and cp["records_total"] == 2
