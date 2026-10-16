"""Engineering domain. Objects link through immutable provider IDs and provenance."""

from __future__ import annotations

from pydantic import Field

from .common import DomainRecord


class Repository(DomainRecord):
    repository_id: str = Field(description="Immutable provider id (GitHub numeric id), not owner/name")
    full_name: str
    default_branch: str | None = None
    private: bool | None = None
    url: str | None = None


class Commit(DomainRecord):
    repository_id: str
    sha: str
    author: str | None = None
    authored_at_iso: str | None = None
    message: str | None = None
    parents: list[str] = Field(default_factory=list)


class PullRequest(DomainRecord):
    repository_id: str
    pr_id: str
    number: int
    title: str
    state: str
    head_sha: str | None = None
    base_ref: str | None = None
    author: str | None = None
    updated_at_iso: str | None = None
    merged_at_iso: str | None = None


class WorkItem(DomainRecord):
    project_id: str
    item_id: str
    key: str
    item_type: str | None
    status: str | None
    summary: str
    updated_at_iso: str | None = None
    assignee: str | None = None


class WorkflowRun(DomainRecord):
    repository_id: str
    run_id: str
    workflow_id: str | None
    head_sha: str | None
    status: str | None
    conclusion: str | None
    created_at_iso: str | None = None
    updated_at_iso: str | None = None
    run_attempt: int | None = None


class WorkflowJob(DomainRecord):
    run_id: str
    job_id: str
    name: str
    status: str | None
    conclusion: str | None
    started_at_iso: str | None = None
    completed_at_iso: str | None = None
    run_attempt: int | None = None


class Artifact(DomainRecord):
    run_id: str
    artifact_id: str
    name: str
    size: int | None = None
    expired: bool | None = None
    expires_at_iso: str | None = None


class Deployment(DomainRecord):
    project_id: str
    deployment_id: str
    environment: str | None
    state: str | None
    url: str | None = None
    commit_sha: str | None = None
    created_at_iso: str | None = None
    ready_at_iso: str | None = None
    error: str | None = None


class Finding(DomainRecord):
    finding_id: str
    source_ref: str
    severity: str | None
    title: str
    status: str | None = None
