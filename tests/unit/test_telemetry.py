from __future__ import annotations

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from wayam_connectors.contracts.errors import AuthError
from wayam_connectors.platform.http import ProviderHttp
from wayam_connectors.platform.security.ssrf import OriginPolicy
from wayam_connectors.platform.telemetry import span


@pytest.fixture(scope="module")
def exporter():
    provider = TracerProvider()
    exp = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(provider)
    return exp


async def test_spans_carry_only_safe_attributes(exporter):
    exporter.clear()
    with span("unit.test", tenant_id="t-1", token="ghp_secret", status="ok"):
        pass

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad"})

    c = ProviderHttp(
        OriginPolicy.for_origins(["https://api.example.com"]),
        transport=httpx.MockTransport(handler),
        resolve_dns=False,
        base_headers={"Authorization": "Bearer s3cret"},
    )
    with pytest.raises(AuthError):
        await c.request("GET", "https://api.example.com/v1/thing?token=abc")
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["unit.test"].attributes == {"tenant_id": "t-1", "status": "ok"}  # unknown keys (token) dropped
    http = spans["provider.http"]
    assert (
        http.attributes["host"] == "api.example.com"
        and http.attributes["path"] == "/v1/thing"
        and http.attributes["http.status_code"] == 401
    )
    assert "token" not in str(http.attributes) and http.status.status_code.name == "ERROR" and http.attributes["error.type"] == "AuthError"
