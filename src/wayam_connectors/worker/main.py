"""Worker loop: claim a job, run its handler under a scoped job grant, ack or fail it.

Handlers receive (runtime, job, grant) and return a result dict. Any ConnectorError
decides retry/fail/dead-letter through JobQueue.fail; unexpected exceptions are
recorded as internal (non-retryable) failures so a bug never loops forever.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from wayam_connectors.contracts.errors import ConnectorError
from wayam_connectors.contracts.identity import JobGrant
from wayam_connectors.platform.jobs import LeasedJob
from wayam_connectors.platform.runtime import Runtime
from wayam_connectors.platform.security.redaction import redact
from wayam_connectors.platform.telemetry import span

log = logging.getLogger("wayam.worker")

Handler = Callable[[Runtime, LeasedJob, JobGrant], dict[str, Any]]


def run_once(rt: Runtime, handlers: dict[str, Handler], worker_id: str, *, tenant_id: UUID | None = None) -> str | None:
    """Claim and run one job. Returns the job's final status or None when the queue is empty."""
    job = rt.jobs.claim(worker_id, tenant_id=tenant_id)
    if job is None:
        return None
    handler = handlers.get(job.job_type)
    if handler is None:
        status = rt.jobs.fail(job, ConnectorError(f"no handler for job_type {job.job_type}"))
        return status
    grant = job.grant(list(job.payload.get("capabilities", [])))
    try:
        with span(
            "worker.job",
            job_id=job.id,
            job_type=job.job_type,
            tenant_id=job.tenant_id,
            connection_id=job.connection_id,
            resource_type=job.resource_type,
            attempt=job.attempts,
            worker_id=worker_id,
        ):
            result = handler(rt, job, grant)
    except ConnectorError as exc:
        status = rt.jobs.fail(job, exc)
        log.warning("job %s failed (%s): %s", job.id, status, redact(exc.to_dict()))
        return status
    except Exception as exc:  # noqa: BLE001 - a bug must not retry forever
        status = rt.jobs.fail(job, ConnectorError(f"unhandled {type(exc).__name__}: {str(exc)[:200]}"))
        log.exception("job %s crashed", job.id)
        return status
    if not rt.jobs.complete(job, result):
        log.warning("job %s: lease lost before completion; result discarded (will re-run)", job.id)
        return "lease_lost"
    if result.get("resume"):
        # page budget spent before the source reported completion: continue from the checkpoint in a new job
        payload = dict(job.payload)
        if result.get("crawl_id"):
            payload["crawl_id"] = result["crawl_id"]
        rt.jobs.enqueue(
            job.tenant_id,
            job.connection_id,
            job.product,
            job_type=job.job_type,
            resource_type=job.resource_type,
            resource_id=job.resource_id,
            payload=payload,
            max_attempts=job.max_attempts,
        )
        return "resumed"
    return "succeeded"


def serve(
    rt: Runtime, handlers: dict[str, Handler], worker_id: str, *, idle_sleep: float = 2.0, stop_after_idle: int | None = None
) -> None:
    idle = 0
    while True:
        status = run_once(rt, handlers, worker_id)
        if status is None:
            idle += 1
            if stop_after_idle is not None and idle >= stop_after_idle:
                return
            time.sleep(idle_sleep)
        else:
            idle = 0


def connection_check_handler(rt: Runtime, job: LeasedJob, grant: JobGrant) -> dict[str, Any]:
    """Built-in job: run ConnectionCheck for a connection and block it on failure."""
    import asyncio

    conn = rt.connections.get_by_ids(job.tenant_id, job.connection_id)
    entry = rt.registry[conn["connector_id"]]
    secret = rt.credentials.get(job.tenant_id, job.connection_id) if entry.manifest.auth_modes != ["inherited"] else {}
    adapter = rt.adapter_for(entry, conn["config"], secret)
    result = asyncio.run(adapter.check_connection())
    if not result.ok:
        rt.connections.set_status(job.tenant_id, job.connection_id, "blocked", result.detail[:500])
    return dict(result.model_dump(mode="json"))


def _default_handlers() -> dict[str, Handler]:
    from .handlers import document_ingest, record_sync, webhook_process

    return {
        "connection_check": connection_check_handler,
        "record_sync": record_sync,
        "webhook_process": webhook_process,
        "document_ingest": document_ingest,
    }


DEFAULT_HANDLERS: dict[str, Handler] = _default_handlers()
