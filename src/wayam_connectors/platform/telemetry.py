"""OpenTelemetry spans for the runtime. Uses the API only: without a configured SDK every span is a no-op.

Attributes never carry secrets or payloads: ids, counts, statuses and connector ids only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

_tracer = trace.get_tracer("wayam_connectors")
_SAFE_KEYS = {
    "tenant_id",
    "connection_id",
    "job_id",
    "job_type",
    "resource_type",
    "resource_id",
    "connector_id",
    "status",
    "method",
    "host",
    "path",
    "http.status_code",
    "pages",
    "inserted",
    "replayed",
    "deleted",
    "quarantined",
    "attempt",
    "worker_id",
    "page_size",
}


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    with _tracer.start_as_current_span(name) as s:
        for k, v in attributes.items():
            if k in _SAFE_KEYS and v is not None:
                s.set_attribute(k, v if isinstance(v, (bool, int, float)) else str(v))
        try:
            yield s
        except Exception as exc:
            s.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            s.set_attribute("error.type", type(exc).__name__)
            raise


def configure_console_exporter() -> None:
    """Development helper: print spans to stdout. Production wiring (OTLP) is deployment configuration."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "wayam-connectors"}))
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
