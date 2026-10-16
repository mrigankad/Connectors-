"""GitHub JSON -> engineering domain records. Immutable numeric ids are the identities; names are attributes."""

from __future__ import annotations

from typing import Any

from wayam_connectors.contracts.domains.engineering import (
    Artifact,
    Commit,
    PullRequest,
    Repository,
    WorkflowJob,
    WorkflowRun,
    WorkItem,
)
from wayam_connectors.contracts.errors import DataError

MAPPING_VERSION = "engineering.github/1"
SRC = "github"


def _req(obj: dict[str, Any], key: str) -> Any:
    if key not in obj or obj[key] is None:
        raise DataError(f"GitHub object lacks required field {key!r}")
    return obj[key]


def map_repository(o: dict[str, Any], observed: str) -> Repository:
    return Repository(
        source_system=SRC,
        source_object_id=str(_req(o, "id")),
        source_version=o.get("pushed_at") or o.get("updated_at"),
        observed_at_iso=observed,
        repository_id=str(o["id"]),
        full_name=_req(o, "full_name"),
        default_branch=o.get("default_branch"),
        private=o.get("private"),
        url=o.get("html_url"),
    )


def map_commit(o: dict[str, Any], repository_id: str, observed: str) -> Commit:
    c = _req(o, "commit")
    author = (c.get("author") or {}).get("name") or (o.get("author") or {}).get("login")
    return Commit(
        source_system=SRC,
        source_object_id=_req(o, "sha"),
        source_version=_req(o, "sha"),
        observed_at_iso=observed,
        repository_id=repository_id,
        sha=o["sha"],
        author=author,
        authored_at_iso=(c.get("author") or {}).get("date"),
        message=c.get("message"),
        parents=[p["sha"] for p in o.get("parents", []) if p.get("sha")],
    )


def map_pull_request(o: dict[str, Any], repository_id: str, observed: str) -> PullRequest:
    return PullRequest(
        source_system=SRC,
        source_object_id=str(_req(o, "id")),
        source_version=_req(o, "updated_at"),
        observed_at_iso=observed,
        repository_id=repository_id,
        pr_id=str(o["id"]),
        number=int(_req(o, "number")),
        title=_req(o, "title"),
        state=_req(o, "state"),
        head_sha=(o.get("head") or {}).get("sha"),
        base_ref=(o.get("base") or {}).get("ref"),
        author=(o.get("user") or {}).get("login"),
        updated_at_iso=o["updated_at"],
        merged_at_iso=o.get("merged_at"),
    )


def map_issue(o: dict[str, Any], repository_id: str, observed: str) -> WorkItem:
    if "pull_request" in o:
        raise DataError("issue payload is a pull request; use the pull_request resource")
    return WorkItem(
        source_system=SRC,
        source_object_id=str(_req(o, "id")),
        source_version=_req(o, "updated_at"),
        observed_at_iso=observed,
        project_id=repository_id,
        item_id=str(o["id"]),
        key=f"#{_req(o, 'number')}",
        item_type="issue",
        status=o.get("state"),
        summary=_req(o, "title"),
        updated_at_iso=o["updated_at"],
        assignee=(o.get("assignee") or {}).get("login"),
    )


def map_workflow_run(o: dict[str, Any], repository_id: str, observed: str) -> WorkflowRun:
    attempt = o.get("run_attempt")
    return WorkflowRun(
        source_system=SRC,
        source_object_id=str(_req(o, "id")),
        source_version=f"{_req(o, 'updated_at')}#{attempt}",
        observed_at_iso=observed,
        repository_id=repository_id,
        run_id=str(o["id"]),
        workflow_id=str(o["workflow_id"]) if o.get("workflow_id") is not None else None,
        head_sha=o.get("head_sha"),
        status=o.get("status"),
        conclusion=o.get("conclusion"),
        created_at_iso=o.get("created_at"),
        updated_at_iso=o.get("updated_at"),
        run_attempt=attempt,
    )


def map_workflow_job(o: dict[str, Any], observed: str) -> WorkflowJob:
    return WorkflowJob(
        source_system=SRC,
        source_object_id=str(_req(o, "id")),
        source_version=f"{o.get('completed_at') or o.get('started_at')}#{o.get('run_attempt')}",
        observed_at_iso=observed,
        run_id=str(_req(o, "run_id")),
        job_id=str(o["id"]),
        name=_req(o, "name"),
        status=o.get("status"),
        conclusion=o.get("conclusion"),
        started_at_iso=o.get("started_at"),
        completed_at_iso=o.get("completed_at"),
        run_attempt=o.get("run_attempt"),
    )


def map_artifact(o: dict[str, Any], run_id: str, observed: str) -> Artifact:
    return Artifact(
        source_system=SRC,
        source_object_id=str(_req(o, "id")),
        source_version=o.get("updated_at"),
        observed_at_iso=observed,
        run_id=run_id,
        artifact_id=str(o["id"]),
        name=_req(o, "name"),
        size=o.get("size_in_bytes"),
        expired=o.get("expired"),
        expires_at_iso=o.get("expires_at"),
    )
