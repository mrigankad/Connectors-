"""Opt-in live tests against api.github.com for mrigankad/Chakram.

Blocked (skipped with reason) unless WAYAM_LIVE_GITHUB=1. Credentials: WAYAM_LIVE_GITHUB_TOKEN (token mode) or
WAYAM_LIVE_GITHUB_AUTH_MODE=ambient when an egress gateway injects them (the Claude build session does this).
Read-only: every request is a GET; the adapters have no write methods.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from wayam_connectors.connectors.github.actions.adapter import GitHubActions
from wayam_connectors.connectors.github.repositories.adapter import GitHubRepositories
from wayam_connectors.contracts.capabilities import SyncRequest
from wayam_connectors.contracts.errors import PolicyError, RetryableError, ScopeError
from wayam_connectors.contracts.identity import AdapterContext, Product

pytestmark = pytest.mark.live
REPO = os.environ.get("WAYAM_LIVE_GITHUB_REPO", "mrigankad/Chakram")


@pytest.fixture(scope="module")
def cfg():
    if os.environ.get("WAYAM_LIVE_GITHUB") != "1":
        pytest.skip("BLOCKED: WAYAM_LIVE_GITHUB=1 not set; live GitHub verification not run")
    mode = os.environ.get("WAYAM_LIVE_GITHUB_AUTH_MODE", "token")
    token = os.environ.get("WAYAM_LIVE_GITHUB_TOKEN")
    if mode == "token" and not token:
        pytest.skip("BLOCKED: WAYAM_LIVE_GITHUB_TOKEN missing")
    return ({"repositories": [REPO], "auth_mode": mode, "require_read_only_credential": False}, {"token": token})


CTX = AdapterContext(tenant_id=uuid4(), connection_id=uuid4(), product=Product.CHAKRAM)


async def test_live_repositories(cfg):
    a = GitHubRepositories(cfg[0], cfg[1], CTX)
    chk = await a.check_connection()
    assert chk.ok, chk.detail
    repos = [r async for r in a.discover()]
    assert repos[0].name == REPO and repos[0].resource_id.isdigit()
    page = await a.sync_page(SyncRequest(resource_type="commit", resource_id=REPO, page_size=5))
    assert page.records and all(len(r.payload["sha"]) == 40 for r in page.records)
    full = await a.sync_page(SyncRequest(resource_type="commit", resource_id=REPO, page_size=100))
    assert full.complete and full.next_cursor["since"]
    again = await a.sync_page(
        SyncRequest(resource_type="commit", resource_id=REPO, page_size=100, cursor=full.next_cursor, overlap_seconds=1)
    )
    assert again.complete and len(again.records) <= 1  # only the watermark commit inside the overlap window
    prs = await a.sync_page(SyncRequest(resource_type="pull_request", resource_id=REPO))
    issues = await a.sync_page(SyncRequest(resource_type="issue", resource_id=REPO))
    assert prs.complete and issues.complete
    with pytest.raises(PolicyError):
        await a.sync_page(SyncRequest(resource_type="commit", resource_id="torvalds/linux"))
    with pytest.raises(ScopeError):
        await GitHubRepositories({**cfg[0], "repositories": ["mrigankad/this-repo-does-not-exist-xyz"]}, cfg[1], CTX).check_connection()
    await a.client.aclose()


async def test_live_actions(cfg):
    a = GitHubActions(cfg[0], cfg[1], CTX)
    chk = await a.check_connection()
    assert chk.ok, chk.detail
    runs = await a.sync_page(SyncRequest(resource_type="workflow_run", resource_id=REPO, page_size=3))
    assert runs.records, "repository has no workflow runs; cannot verify"
    run_id = runs.records[0].payload["run_id"]
    jobs = await a.sync_page(SyncRequest(resource_type="workflow_job", resource_id=f"{REPO}#{run_id}"))
    assert jobs.complete and jobs.records and all(j.payload["run_id"] == run_id for j in jobs.records)
    arts = await a.sync_page(SyncRequest(resource_type="artifact", resource_id=f"{REPO}#{run_id}"))
    assert arts.complete
    await a.client.aclose()


async def test_live_actions_logs(cfg):
    """Log download follows a redirect to GitHub's results storage origin; some egress policies block it."""
    a = GitHubActions(cfg[0], cfg[1], CTX)
    runs = await a.sync_page(SyncRequest(resource_type="workflow_run", resource_id=REPO, page_size=10))
    completed = [r for r in runs.records if r.payload["status"] == "completed"]  # logs 404 while a run is in progress (observed)
    if not completed:
        await a.client.aclose()
        pytest.skip("BLOCKED: no completed workflow run available to fetch logs for")
    run_id = completed[0].payload["run_id"]
    try:
        doc = await a.fetch_document(f"{REPO}#run:{run_id}")
    except RetryableError as exc:
        pytest.skip(f"BLOCKED: log storage origin unreachable from this network ({exc.message}); log fetch not live-verified")
    finally:
        await a.client.aclose()
    assert doc.size > 0 and b"=====" in doc.content
