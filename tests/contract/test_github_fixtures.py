"""Contract tests for github.repositories / github.actions against captured real responses (fixtures/github).

Fixtures repo.json, commits_page1.json, runs.json, jobs.json, artifacts.json, workflows.json and *.headers were
captured live from api.github.com for mrigankad/Chakram on 2026-10-16 (emails redacted). pulls_synthetic.json and
issues_synthetic.json are SYNTHETIC (the repository had no PRs/issues) and are labelled as such inside the files.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from tests.conftest import FIXTURES
from wayam_connectors.connectors.github.actions.adapter import GitHubActions
from wayam_connectors.connectors.github.repositories.adapter import GitHubRepositories
from wayam_connectors.contracts.capabilities import SyncRequest, WebhookDelivery
from wayam_connectors.contracts.errors import (
    AuthError,
    DataError,
    IntegrityError,
    PolicyError,
    RateLimitedError,
    ScopeError,
    UnsupportedOperationError,
    ValidationError,
)
from wayam_connectors.contracts.identity import AdapterContext, Product

GH = FIXTURES / "github"
CTX = AdapterContext(tenant_id=uuid4(), connection_id=uuid4(), product=Product.CHAKRAM)
CFG = {"repositories": ["mrigankad/Chakram"], "auth_mode": "token", "require_read_only_credential": False}
SECRET = {"token": "ghp_fixture_token_000000000000000000000000"}


def _load(name: str):
    return json.loads((GH / name).read_text())


def _headers(name: str) -> dict[str, str]:
    out = {}
    for line in (GH / name).read_text().splitlines():
        if ":" in line and not line.startswith("HTTP/"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


class Fake:
    """Serves the fixtures; records requests; can inject failures."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, httpx.Response] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.method == "GET"
        if req.url.host == "api.github.com":
            assert req.headers["X-GitHub-Api-Version"] == "2022-11-28" and req.headers["Accept"] == "application/vnd.github+json"
        p = req.url.path
        if p in self.fail:
            return self.fail[p]
        q = dict(req.url.params)
        if p == "/repos/mrigankad/Chakram":
            return httpx.Response(200, json=_load("repo.json"), headers=_headers("repo.headers"))
        if p == "/repos/mrigankad/Chakram/commits":
            if q.get("page") == "2":
                return httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "100"})
            return httpx.Response(200, json=_load("commits_page1.json"), headers=_headers("commits.headers"))
        if p == "/repositories/1422286600/commits":  # Link rel=next form observed live
            return httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": "100"})
        if p == "/repos/mrigankad/Chakram/pulls":
            return httpx.Response(200, json=_load("pulls_synthetic.json"), headers=_headers("pulls.headers"))
        if p == "/repos/mrigankad/Chakram/issues":
            return httpx.Response(200, json=_load("issues_synthetic.json"))
        if p == "/repos/mrigankad/Chakram/actions/runs":
            return httpx.Response(200, json=_load("runs.json"), headers=_headers("runs.headers"))
        if p == "/repositories/1422286600/actions/runs":
            return httpx.Response(200, json={"total_count": 7, "workflow_runs": []})
        if p == "/repos/mrigankad/Chakram/actions/runs/38933603434/jobs":
            return httpx.Response(200, json=_load("jobs.json"))
        if p == "/repos/mrigankad/Chakram/actions/runs/38933603434/artifacts":
            return httpx.Response(200, json=_load("artifacts.json"))
        if p == "/repos/mrigankad/Chakram/actions/workflows":
            return httpx.Response(200, json=_load("workflows.json"))
        if p == "/repos/mrigankad/Chakram/actions/runs/38933603434/logs":
            return httpx.Response(302, headers={"Location": "https://results-receiver.actions.githubusercontent.com/x?sig=abc"})
        if req.url.host == "results-receiver.actions.githubusercontent.com":
            assert "authorization" not in req.headers or req.headers["authorization"] == ""
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("1_build.txt", "2026-10-16T16:39:01Z Run npm ci\ntoken=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
                zf.writestr("2_test.txt", "ok\n")
                zf.writestr("meta.json", "{}")
            return httpx.Response(200, content=buf.getvalue())
        if p == "/repos/mrigankad/Chakram/actions/runs/1/logs":
            return httpx.Response(410, json={"message": "Gone"})
        return httpx.Response(404, json={"message": "Not Found"})


def _repos(fake: Fake, **over) -> GitHubRepositories:
    return GitHubRepositories({**CFG, **over}, SECRET, CTX, transport=httpx.MockTransport(fake), resolve_dns=False)


def _actions(fake: Fake, **over) -> GitHubActions:
    return GitHubActions({**CFG, **over}, SECRET, CTX, transport=httpx.MockTransport(fake), resolve_dns=False)


async def test_check_discover_and_write_capable_credential():
    fake = Fake()
    res = await _repos(fake, require_read_only_credential=True).check_connection()
    assert not res.ok and "write-capable" in res.detail  # captured repo.json shows admin/push permissions
    res = await _repos(fake).check_connection()
    assert res.ok and "15000" in res.detail
    assert fake.requests[0].headers["Authorization"] == "Bearer " + SECRET["token"]
    found = [r async for r in _repos(fake).discover()]
    assert [(r.resource_id, r.name) for r in found] == [("1422286600", "mrigankad/Chakram")]
    with pytest.raises(UnsupportedOperationError):
        _ = [r async for r in _repos(fake).discover("issue")]


async def test_allowlist_and_read_only_boundaries():
    fake = Fake()
    a = _repos(fake)
    with pytest.raises(PolicyError):
        await a.sync_page(SyncRequest(resource_type="commit", resource_id="mrigankad/Connectors-"))
    with pytest.raises(ValidationError):
        GitHubRepositories({**CFG, "repositories": ["bad name"]}, SECRET, CTX)
    with pytest.raises(ValidationError):
        GitHubRepositories(CFG, {"token": None}, CTX)  # token mode needs a token
    assert not hasattr(a, "perform_action") and not hasattr(_actions(fake), "perform_action")  # T-44
    with pytest.raises(PolicyError):
        await a.client.http.request("POST", "https://api.github.com/repos/mrigankad/Chakram/actions/runs/1/rerun")
    assert all(r.method == "GET" for r in fake.requests)


async def test_sync_repository_commits_prs_issues_and_cursor():
    fake = Fake()
    a = _repos(fake)
    page = await a.sync_page(SyncRequest(resource_type="repository", resource_id="mrigankad/Chakram"))
    assert (
        page.complete and page.records[0].payload["repository_id"] == "1422286600" and page.records[0].payload["default_branch"] == "main"
    )
    assert page.records[0].tenant_id == CTX.tenant_id and page.records[0].source_object_type == "engineering.repository"

    commits = await a.sync_page(SyncRequest(resource_type="commit", resource_id="mrigankad/Chakram", page_size=2))
    assert len(commits.records) == 2 and not commits.complete and commits.next_cursor["next_url"].endswith("commits?per_page=2&page=2")
    c0 = commits.records[0]
    assert c0.source_object_id == c0.payload["sha"] == c0.source_version and len(c0.payload["sha"]) == 40
    assert c0.extensions["github"]["commit"]["author"]["email"] == "redacted@example.invalid"
    assert c0.provenance.startswith("https://api.github.com/repos/mrigankad/Chakram/commits/")
    nxt = await a.sync_page(SyncRequest(resource_type="commit", resource_id="mrigankad/Chakram", page_size=2, cursor=commits.next_cursor))
    assert nxt.complete and nxt.records == [] and nxt.next_cursor["next_url"] is None and nxt.next_cursor["since"] is not None
    assert fake.requests[-1].url.path == "/repositories/1422286600/commits"  # resumed from the Link URL, not rebuilt
    with pytest.raises(ValidationError):
        await a.sync_page(SyncRequest(resource_type="commit", resource_id="mrigankad/Chakram", cursor={"v": 2}))

    prs = await a.sync_page(SyncRequest(resource_type="pull_request", resource_id="mrigankad/Chakram"))
    assert [r.payload["number"] for r in prs.records] == [7, 8] and prs.records[1].payload["merged_at_iso"] == "2026-10-12T10:30:00Z"
    assert prs.records[0].source_version == "2026-10-12T10:30:00Z" and "_fixture_note" not in prs.records[0].extensions["github"]
    assert prs.complete and prs.next_cursor["since"] == "2026-10-12T10:30:00+00:00"
    # watermark + overlap: items older than since-overlap are skipped client-side
    later = await a.sync_page(
        SyncRequest(
            resource_type="pull_request",
            resource_id="mrigankad/Chakram",
            cursor={"v": 1, "next_url": None, "since": "2026-10-13T00:00:00+00:00"},
            overlap_seconds=60,
        )
    )
    assert later.records == []
    overlap = await a.sync_page(
        SyncRequest(
            resource_type="pull_request",
            resource_id="mrigankad/Chakram",
            cursor={"v": 1, "next_url": None, "since": "2026-10-12T10:30:30+00:00"},
            overlap_seconds=60,
        )
    )
    assert len(overlap.records) == 2  # equal timestamps inside the overlap window are re-read, not lost (T-10/T-11)

    issues = await a.sync_page(SyncRequest(resource_type="issue", resource_id="mrigankad/Chakram"))
    assert [r.payload["key"] for r in issues.records] == ["#9"] and len(
        issues.partial_failures
    ) == 1  # the PR row is reported, not stored as an issue
    assert issues.partial_failures[0]["error"]["class"] == "data"
    with pytest.raises(UnsupportedOperationError):
        await a.sync_page(SyncRequest(resource_type="release", resource_id="mrigankad/Chakram"))


async def test_error_mapping_and_pagination_cycle():
    fake = Fake()
    a = _repos(fake)
    fake.fail["/repos/mrigankad/Chakram"] = httpx.Response(401, json={"message": "Bad credentials"})
    with pytest.raises(AuthError):
        await a.check_connection()
    fake.fail["/repos/mrigankad/Chakram"] = httpx.Response(
        403, json={"message": "API rate limit exceeded"}, headers={"X-RateLimit-Remaining": "0", "Retry-After": "12"}
    )
    with pytest.raises(RateLimitedError) as rl:
        await a.check_connection()
    assert rl.value.retry_after_seconds == 12.0
    fake.fail["/repos/mrigankad/Chakram"] = httpx.Response(404, json={"message": "Not Found"})
    with pytest.raises(ScopeError):
        await a.check_connection()
    fake.fail.clear()
    fake.fail["/repos/mrigankad/Chakram/commits"] = httpx.Response(
        200, json=[], headers={"Link": '<https://api.github.com/repos/mrigankad/Chakram/commits?per_page=2>; rel="next"'}
    )
    with pytest.raises(IntegrityError, match="cycle"):
        async for _ in a.client.paginate("/repos/mrigankad/Chakram/commits", {"per_page": 2}, max_pages=5):
            pass


async def test_actions_runs_jobs_artifacts_logs():
    fake = Fake()
    a = _actions(fake)
    chk = await a.check_connection()
    assert chk.ok and "1" in chk.detail
    runs = await a.sync_page(SyncRequest(resource_type="workflow_run", resource_id="mrigankad/Chakram", page_size=2))
    assert len(runs.records) == 2 and runs.source_counts["total_count"] == 7 and not runs.complete
    r0 = runs.records[0].payload
    assert (
        r0["run_id"] == "38933603434"
        and r0["conclusion"] == "success"
        and r0["workflow_id"] == "386796586"
        and runs.records[0].source_version.endswith("#1")
    )
    jobs = await a.sync_page(SyncRequest(resource_type="workflow_job", resource_id="mrigankad/Chakram#38933603434"))
    assert jobs.complete and len(jobs.records) == 3 and {j.payload["name"] for j in jobs.records} >= {"Vercel entrypoint imports"}
    arts = await a.sync_page(SyncRequest(resource_type="artifact", resource_id="mrigankad/Chakram#38933603434"))
    assert arts.records == [] and arts.source_counts["total_count"] == 0
    with pytest.raises(ValidationError):
        await a.sync_page(SyncRequest(resource_type="workflow_job", resource_id="mrigankad/Chakram"))
    with pytest.raises(PolicyError):
        await a.sync_page(SyncRequest(resource_type="workflow_job", resource_id="other/repo#1"))
    # since cursor adds the created filter
    await a.sync_page(
        SyncRequest(
            resource_type="workflow_run",
            resource_id="mrigankad/Chakram",
            cursor={"v": 1, "next_url": None, "since": "2026-10-15T00:00:00+00:00"},
        )
    )
    assert dict(fake.requests[-1].url.params)["created"] == ">=2026-10-15"
    # logs: redirect to allowlisted storage without the token, zip inspected, secrets redacted
    doc = await a.fetch_document("mrigankad/Chakram#run:38933603434")
    text = doc.content.decode()
    assert "===== 1_build.txt =====" in text and "Run npm ci" in text and "ghp_" not in text and "meta.json" not in text
    assert doc.content_type.startswith("text/plain") and doc.version_id == "38933603434"
    with pytest.raises(PolicyError):
        await _actions(fake, log_download_origins=["https://other.example.net"]).fetch_document("mrigankad/Chakram#run:38933603434")
    with pytest.raises(DataError, match="expired"):
        await a.fetch_document("mrigankad/Chakram#run:1")
    with pytest.raises(ValidationError):
        await a.fetch_document("mrigankad/Chakram#run:x")


def test_webhook_signature_and_events():
    a = _repos(Fake())
    secret = b"hook-secret"
    body = json.dumps(
        {
            "action": "opened",
            "repository": {"id": 1422286600, "full_name": "mrigankad/Chakram"},
            "pull_request": _load("pulls_synthetic.json")[0],
        }
    ).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    now = datetime.now(UTC)
    good = WebhookDelivery(
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-1", "X-GitHub-Event": "pull_request"}, body=body, received_at=now
    )
    v = a.verify_delivery(good, secret)
    assert v.verified and v.delivery_id == "d-1" and v.event_type == "pull_request"
    assert not a.verify_delivery(
        WebhookDelivery(headers={**good.headers, "X-Hub-Signature-256": "sha256=00"}, body=body, received_at=now), secret
    ).verified
    assert not a.verify_delivery(WebhookDelivery(headers=good.headers, body=body + b" ", received_at=now), secret).verified  # T-24
    envs = a.envelopes_from_delivery(good)
    assert (
        len(envs) == 1
        and envs[0].event_id == "d-1"
        and envs[0].payload["number"] == 7
        and envs[0].source_object_type == "engineering.pull_request"
    )
    # events for a repository outside the allowlist are rejected
    other = json.dumps({"repository": {"id": 1, "full_name": "evil/repo"}, "pull_request": _load("pulls_synthetic.json")[0]}).encode()
    with pytest.raises(PolicyError):
        a.envelopes_from_delivery(
            WebhookDelivery(headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-2"}, body=other, received_at=now)
        )
    deleted = json.dumps(
        {
            "action": "deleted",
            "repository": {"id": 1422286600, "full_name": "mrigankad/Chakram"},
            "issue": _load("issues_synthetic.json")[0],
        }
    ).encode()
    envs = a.envelopes_from_delivery(
        WebhookDelivery(headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-3"}, body=deleted, received_at=now)
    )
    assert envs[0].operation.value == "delete" and envs[0].payload["key"] == "#9"
    ping = a.envelopes_from_delivery(
        WebhookDelivery(
            headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d-4"},
            body=json.dumps({"repository": {"id": 1422286600, "full_name": "mrigankad/Chakram"}}).encode(),
            received_at=now,
        )
    )
    assert ping == []
