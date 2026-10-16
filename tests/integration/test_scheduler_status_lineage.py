"""Scheduler tick, connection status (freshness/lag/failures) and Chakram lineage over stored records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_DSN, TEST_KEY, make_tenant
from tests.integration.test_worker_sync import FixtureGitHub, MemorySource, _manifest
from wayam_connectors.api.app import create_app
from wayam_connectors.contracts.errors import PolicyError, ValidationError
from wayam_connectors.contracts.identity import Product
from wayam_connectors.platform.config import Settings
from wayam_connectors.platform.registry import ConnectorEntry, sync_definitions
from wayam_connectors.platform.runtime import build_runtime
from wayam_connectors.worker.main import DEFAULT_HANDLERS, run_once

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def rt(store, tmp_path_factory):
    settings = Settings(database_url=TEST_DSN, local_secret_key=TEST_KEY, secret_backend="local")
    r = build_runtime(settings, dsn=TEST_DSN, raw_root=tmp_path_factory.mktemp("raw"))
    r.registry["test.memory"] = ConnectorEntry(
        manifest=_manifest("test.memory", "full_crawl_reconcile"), adapter_cls=MemorySource, package="tests"
    )
    gh = r.registry["github.repositories"]
    r.registry["github.repositories"] = ConnectorEntry(manifest=gh.manifest, adapter_cls=FixtureGitHub, package=gh.package)
    sync_definitions(r.store, r.registry)
    return r


def _drain(rt, t, limit=20):
    out = []
    for _ in range(limit):
        s = run_once(rt, DEFAULT_HANDLERS, "w", tenant_id=t.tenant_id)
        if s is None:
            break
        out.append(s)
    return out


def test_scheduler_enqueues_due_jobs_once_and_respects_active_and_blocked(rt, tenants):
    t = make_tenant(tenants, "sched-tenant", Product.CHAKRAM)
    cid = rt.connections.create(t.admin, "test.memory", "c", {})
    rt.grants.grant(t.admin, cid, Product.CHAKRAM, ["RecordSync"])
    with pytest.raises(PolicyError):
        rt.scheduler.create(t.product_ctx, cid, job_type="record_sync", resource_type="object", resource_id=None, interval_seconds=120)
    with pytest.raises(ValidationError):
        rt.scheduler.create(t.admin, cid, job_type="record_sync", resource_type="object", resource_id=None, interval_seconds=10)
    with pytest.raises(ValidationError):
        rt.scheduler.create(t.admin, cid, job_type="teleport", resource_type="object", resource_id=None, interval_seconds=120)
    with pytest.raises(PolicyError):  # product without the capability on this connection
        rt.scheduler.create(
            t.admin, cid, job_type="record_sync", resource_type="object", resource_id=None, interval_seconds=120, product=Product.BODHAM
        )
    sid = rt.scheduler.create(
        t.admin,
        cid,
        job_type="record_sync",
        resource_type="object",
        resource_id=None,
        interval_seconds=120,
        payload={"page_size": 2},
        product=Product.CHAKRAM,
    )
    now = datetime.now(UTC)
    first = rt.scheduler.tick(now=now)
    assert first["enqueued"] >= 1
    sched = rt.scheduler.list(t.admin, cid)[0]
    assert sched["last_job_id"] is not None and sched["next_run_at"] > now
    # not due again yet; and while the job is active a second due tick skips rather than duplicating
    assert rt.scheduler.tick(now=now)["enqueued"] == 0
    again = rt.scheduler.tick(now=now + timedelta(seconds=121))
    assert again["skipped_active"] >= 1
    MemorySource.objects = {"a": {"v": "1"}}
    assert _drain(rt, t) == ["succeeded"]
    later = rt.scheduler.tick(now=now + timedelta(seconds=300))
    assert later["enqueued"] >= 1 and _drain(rt, t) == ["succeeded"]
    assert len([j for j in rt.jobs.list(t.tenant_id, cid) if j["status"] == "succeeded"]) == 2
    # a blocked connection is skipped
    rt.connections.set_status(t.tenant_id, cid, "blocked", "credentials revoked")
    blocked = rt.scheduler.tick(now=now + timedelta(seconds=600))
    assert blocked["skipped_blocked"] >= 1
    rt.connections.set_status(t.tenant_id, cid, "active", "restored")
    assert rt.scheduler.set_enabled(t.admin, sid, False) and rt.scheduler.tick(now=now + timedelta(seconds=900))["due"] == 0
    with pytest.raises(PolicyError):
        rt.scheduler.set_enabled(t.product_ctx, sid, True)


def test_connection_status_reports_lag_counts_and_failures(rt, tenants):
    t = make_tenant(tenants, "status-tenant", Product.CHAKRAM)
    cid = rt.connections.create(t.admin, "test.memory", "c", {})
    rt.grants.grant(t.admin, cid, Product.CHAKRAM, ["RecordSync"])
    empty = rt.status.connection_status(t.product_ctx, cid)
    assert empty["evaluated"] is False and empty["partitions"] == [] and empty["records"] == {} and empty["acl"]["documents"] == 0
    MemorySource.objects = {"a": {"v": "1"}, "b": {"v": "1"}, "c": {"v": "1"}}
    rt.jobs.enqueue(t.tenant_id, cid, Product.CHAKRAM, job_type="record_sync", resource_type="object")
    _drain(rt, t)
    MemorySource.fail_on_page = 0
    rt.jobs.enqueue(t.tenant_id, cid, Product.CHAKRAM, job_type="record_sync", resource_type="object", max_attempts=1)
    assert _drain(rt, t) == ["dead_letter"]
    now = datetime.now(UTC) + timedelta(seconds=90)
    st = rt.status.connection_status(t.product_ctx, cid, now=now)
    assert st["evaluated"] and st["status"] == "active"
    p = st["partitions"][0]
    assert p["resource_type"] == "object" and p["records_total"] == 3 and 85 <= p["lag_seconds"] <= 100 and p["has_cursor"]
    assert st["records"] == {"test.object": {"current": 3, "deleted": 0}}
    assert st["jobs"] == {"succeeded": 1, "dead_letter": 1}
    assert st["recent_failures"][0]["status"] == "dead_letter" and st["recent_failures"][0]["error"] == "source hiccup"
    assert st["quarantine"] == {"partial_failure": 1}  # page 0 reports one unmappable row
    with pytest.raises(PolicyError):
        rt.status.connection_status(make_tenant(tenants, "other", Product.CHAKRAM).product_ctx, cid)


def test_chakram_lineage_commit_to_ci(rt, tenants):
    t = make_tenant(tenants, "lineage-tenant", Product.CHAKRAM)
    cfg = {"repositories": ["mrigankad/Chakram"], "auth_mode": "token", "require_read_only_credential": False}
    cid = rt.connections.create(t.admin, "github.repositories", "repo", cfg)
    rt.credentials.put(t.tenant_id, cid, {"token": "ghp_fixture"})
    rt.grants.grant(t.admin, cid, Product.CHAKRAM, ["RecordSync"])
    # Actions fixtures through the same captured transport (separate connection, as in production)
    import httpx

    from tests.contract.test_github_fixtures import Fake
    from wayam_connectors.connectors.github.actions.adapter import GitHubActions

    class FixtureActions(GitHubActions):
        def __init__(self, config, secret, context=None):
            super().__init__(config, secret, context, transport=httpx.MockTransport(Fake()), resolve_dns=False)

    act = rt.registry["github.actions"]
    rt.registry["github.actions"] = ConnectorEntry(manifest=act.manifest, adapter_cls=FixtureActions, package=act.package)
    cid2 = rt.connections.create(t.admin, "github.actions", "ci", cfg)
    rt.credentials.put(t.tenant_id, cid2, {"token": "ghp_fixture"})
    rt.grants.grant(t.admin, cid2, Product.CHAKRAM, ["RecordSync"])
    import json as _json

    from tests.conftest import FIXTURES

    main_run = _json.loads((FIXTURES / "github" / "runs_main.json").read_text())["workflow_runs"][0]
    sha = "3c2dbb424ba3d4f71e8521925b49bc901505094b"  # captured HEAD of main (commits_page1.json); runs_main.json is its CI run
    assert main_run["head_sha"] == sha
    for rtype in ("commit", "pull_request"):
        rt.jobs.enqueue(
            t.tenant_id,
            cid,
            Product.CHAKRAM,
            job_type="record_sync",
            resource_type=rtype,
            resource_id="mrigankad/Chakram",
            payload={"page_size": 2},
        )
    rt.jobs.enqueue(
        t.tenant_id,
        cid2,
        Product.CHAKRAM,
        job_type="record_sync",
        resource_type="workflow_run",
        resource_id="mrigankad/Chakram",
        payload={"page_size": 2},
    )
    rt.jobs.enqueue(
        t.tenant_id,
        cid2,
        Product.CHAKRAM,
        job_type="record_sync",
        resource_type="workflow_job",
        resource_id=f"mrigankad/Chakram#{main_run['id']}",
    )
    assert all(s == "succeeded" for s in _drain(rt, t))
    lin = rt.lineage.for_commit(t.product_ctx, sha[:12])
    assert lin["found"] and lin["commits"][0]["sha"] == sha and lin["repository_ids"] == ["1422286600"]
    assert [r["run_id"] for r in lin["workflow_runs"]] == [str(main_run["id"])]
    assert lin["workflow_runs"][0]["conclusion"] == main_run["conclusion"]
    assert len(lin["workflow_runs"][0]["jobs"]) == 3 and lin["pull_requests"] == []  # synthetic PRs point at other shas
    assert lin["deployments"] == [] and "not evaluated" in lin["evaluated"]["deployments"]
    pr_sha = "a" * 40
    assert rt.lineage.for_commit(t.product_ctx, pr_sha)["found"] is False  # no commit record for the synthetic PR head
    with pytest.raises(ValidationError):
        rt.lineage.for_commit(t.product_ctx, "zz")
    # API: lineage limited to connections the calling product holds grants on; another tenant sees nothing
    c = TestClient(create_app(rt))
    r = c.get(f"/lineage/commits/{sha}", headers={"Authorization": f"Bearer {t.raw_key}"})
    assert r.status_code == 200 and r.json()["found"] and len(r.json()["workflow_runs"]) == 1
    other = make_tenant(tenants, "stranger", Product.CHAKRAM)
    assert c.get(f"/lineage/commits/{sha}", headers={"Authorization": f"Bearer {other.raw_key}"}).json()["found"] is False
    st = c.get(f"/connections/{cid2}/status", headers={"Authorization": f"Bearer {t.raw_key}"}).json()
    assert st["records"]["engineering.workflow_run"]["current"] == 3 and st["jobs"] == {"succeeded": 2}
