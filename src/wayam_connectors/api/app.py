"""Control API. Tenant identity is derived from the bearer API key on every request."""

import base64
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from wayam_connectors.contracts.capabilities import Capability, CheckResult, QueryRequest, QueryResult
from wayam_connectors.contracts.errors import (
    AuthError,
    ConnectorError,
    DataError,
    ErrorClass,
    NotConfiguredError,
    PolicyError,
    UnsupportedOperationError,
    ValidationError,
)
from wayam_connectors.contracts.identity import AdapterContext, Product, TenantContext
from wayam_connectors.platform.runtime import Runtime, default_runtime
from wayam_connectors.platform.security.redaction import redact

_STATUS = {
    ErrorClass.AUTH: 401,
    ErrorClass.SCOPE: 403,
    ErrorClass.POLICY: 403,
    ErrorClass.UNSUPPORTED: 405,
    ErrorClass.VALIDATION: 422,
    ErrorClass.DATA: 422,
    ErrorClass.INTEGRITY: 409,
    ErrorClass.CURSOR_EXPIRED: 409,
    ErrorClass.RATE_LIMITED: 429,
    ErrorClass.RETRYABLE: 502,
    ErrorClass.INTERNAL: 500,
}


def create_app(runtime: Runtime | None = None) -> FastAPI:
    app = FastAPI(title="Wayam Connector Platform", version="0.1.0", docs_url="/docs", openapi_url="/openapi.json")
    rt_holder = {"rt": runtime}

    def rt() -> Runtime:
        if rt_holder["rt"] is None:
            rt_holder["rt"] = default_runtime()
        return rt_holder["rt"]  # type: ignore[return-value]

    def current(authorization: Annotated[str | None, Header()] = None) -> TenantContext:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("missing bearer API key")
        return rt().tenants.authenticate(authorization.split(" ", 1)[1].strip())

    Ctx = Annotated[TenantContext, Depends(current)]

    @app.exception_handler(ConnectorError)
    async def _connector_error(_: Request, exc: ConnectorError) -> JSONResponse:
        # AuthError on an API key is a 401 from the auth layer; the same class from a provider is still 401 (connection blocked)
        return JSONResponse(status_code=_STATUS.get(exc.error_class, 500), content=redact(exc.to_dict()))

    # ------------------------------------------------------------ models

    class ConnectionCreate(BaseModel):
        connector_id: str
        name: str
        config: dict[str, Any] = Field(default_factory=dict)
        secret: dict[str, Any] | None = None

    class GrantCreate(BaseModel):
        product: Product
        capabilities: list[Capability]
        resource_allowlist: list[dict[str, Any]] = Field(default_factory=list)

    class QueryBody(BaseModel):
        sql: str
        params: dict[str, Any] = Field(default_factory=dict)
        max_rows: int | None = None
        timeout_seconds: float = 10.0
        user_context: str | None = None

    class ImportBody(BaseModel):
        connector_id: str
        source_ref: str
        content_base64: str
        parse_options: dict[str, Any] | None = None
        domain_mapping: str
        mapping_config: dict[str, Any] | None = None
        control_totals: dict[str, Any] | None = None
        allow_duplicate_input: bool = False

    # ------------------------------------------------------------ routes

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "connectors": sorted(rt().registry)}

    @app.get("/connectors")
    def list_connectors(ctx: Ctx) -> list[dict[str, Any]]:
        return [e.manifest.model_dump(mode="json") for e in rt().registry.values()]

    @app.get("/connectors/{connector_id}")
    def get_connector(ctx: Ctx, connector_id: str) -> dict[str, Any]:
        e = rt().registry.get(connector_id)
        if e is None:
            raise ValidationError("unknown connector", details={"connector_id": connector_id})
        return e.manifest.model_dump(mode="json")

    @app.post("/connections", status_code=201)
    def create_connection(ctx: Ctx, body: ConnectionCreate) -> dict[str, Any]:
        r = rt()
        entry = r.registry.get(body.connector_id)
        if entry is None:
            raise ValidationError("unknown connector", details={"connector_id": body.connector_id})
        if entry.manifest.capabilities != [Capability.FILE_PARSER]:
            # validate config/secret shape by instantiating the adapter (no network)
            r.adapter_for(entry, body.config, body.secret or {})
        cid = r.connections.create(ctx, body.connector_id, body.name, body.config)
        if body.secret:
            r.credentials.put(ctx.tenant_id, cid, body.secret)
        return {"id": str(cid), "connector_id": body.connector_id, "name": body.name}

    @app.get("/connections")
    def list_connections(ctx: Ctx) -> list[dict[str, Any]]:
        return [_json(c) for c in rt().connections.list(ctx)]

    @app.get("/connections/{connection_id}")
    def get_connection(ctx: Ctx, connection_id: UUID) -> dict[str, Any]:
        return _json(rt().connections.get(ctx, connection_id))

    @app.post("/connections/{connection_id}/grants", status_code=201)
    def grant(ctx: Ctx, connection_id: UUID, body: GrantCreate) -> dict[str, Any]:
        r = rt()
        conn = r.connections.get(ctx, connection_id)
        manifest = r.registry[conn["connector_id"]].manifest
        for cap in body.capabilities:
            if not manifest.supports(cap):
                raise UnsupportedOperationError("connector does not declare capability", details={"capability": cap.value})
        gid = r.grants.grant(ctx, connection_id, body.product, [c.value for c in body.capabilities], body.resource_allowlist)
        return {"id": str(gid)}

    @app.post("/connections/{connection_id}/revoke")
    def revoke(ctx: Ctx, connection_id: UUID) -> dict[str, Any]:
        if "admin" not in ctx.roles:
            raise PolicyError("only tenant admins may revoke connections")
        rt().connections.get(ctx, connection_id)
        rt().credentials.revoke(ctx.tenant_id, connection_id, f"revoked by {ctx.principal_id}")
        return {"status": "revoked"}

    def _adapter(ctx: TenantContext, connection_id: UUID, capability: Capability) -> tuple[Any, Any]:
        r = rt()
        r.grants.require(ctx, connection_id, capability.value)
        conn = r.connections.get(ctx, connection_id)
        entry = r.registry[conn["connector_id"]]
        if not entry.manifest.supports(capability):
            raise UnsupportedOperationError("connector does not declare capability", details={"capability": capability.value})
        secret = r.credentials.get(ctx.tenant_id, connection_id) if entry.manifest.auth_modes != ["inherited"] else {}
        return entry, r.adapter_for(entry, conn["config"], secret)

    @app.post("/connections/{connection_id}/check")
    async def check(ctx: Ctx, connection_id: UUID) -> dict[str, Any]:
        _, adapter = _adapter(ctx, connection_id, Capability.CONNECTION_CHECK)
        result: CheckResult = await adapter.check_connection()
        if not result.ok:
            rt().connections.set_status(ctx.tenant_id, connection_id, "blocked", result.detail[:500])
        elif rt().connections.get(ctx, connection_id)["status"] == "blocked":
            rt().connections.set_status(ctx.tenant_id, connection_id, "active", "check passed")
        return result.model_dump(mode="json")

    @app.get("/connections/{connection_id}/resources")
    async def resources(ctx: Ctx, connection_id: UUID, resource_type: str | None = None) -> list[dict[str, Any]]:
        _, adapter = _adapter(ctx, connection_id, Capability.RESOURCE_DISCOVERY)
        return [r.model_dump(mode="json", by_alias=True) async for r in adapter.discover(resource_type)]

    @app.post("/connections/{connection_id}/query")
    async def query(ctx: Ctx, connection_id: UUID, body: QueryBody) -> dict[str, Any]:
        _, adapter = _adapter(ctx, connection_id, Capability.BOUNDED_QUERY)
        res: QueryResult = await adapter.query(QueryRequest(**body.model_dump()))
        return res.model_dump(mode="json")

    @app.post("/connections/{connection_id}/imports")
    def import_file(ctx: Ctx, connection_id: UUID, body: ImportBody) -> dict[str, Any]:
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except Exception as exc:
            raise ValidationError("content_base64 is not valid base64") from exc
        return rt().file_import.import_file(
            ctx,
            connection_id,
            connector_id=body.connector_id,
            data=data,
            source_ref=body.source_ref,
            parse_options=body.parse_options,
            domain_mapping=body.domain_mapping,
            mapping_config=body.mapping_config,
            control_totals=body.control_totals,
            allow_duplicate_input=body.allow_duplicate_input,
        )

    @app.post("/webhooks/{connection_id}")
    async def webhook(connection_id: UUID, request: Request, x_wayam_tenant: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        """Provider webhook ingress. The tenant comes from the registered webhook route (X-Wayam-Tenant is the
        tenant id embedded in the URL the operator registered at the provider), the signature is verified over the
        raw bytes with the connection's webhook secret, and the delivery is stored before acknowledgement."""
        r = rt()
        if not x_wayam_tenant:
            raise AuthError("missing tenant route header")
        try:
            tenant_id = UUID(x_wayam_tenant)
        except ValueError as exc:
            raise AuthError("invalid tenant route header") from exc
        conn = r.connections.get_by_ids(tenant_id, connection_id)
        entry = r.registry[conn["connector_id"]]
        if not entry.manifest.supports(Capability.WEBHOOK_RECEIVER):
            raise UnsupportedOperationError("connector has no webhook receiver")
        secret = r.credentials.get(tenant_id, connection_id)
        hook_secret = secret.get("webhook_secret")
        if not hook_secret:
            raise AuthError("connection has no webhook secret configured")
        receiver = r.adapter_for(
            entry,
            conn["config"],
            secret,
            context=AdapterContext(tenant_id=tenant_id, connection_id=connection_id, product=Product(conn.get("product") or "platform")),
        )
        body = await request.body()
        return r.webhooks.receive(tenant_id, connection_id, receiver, headers=dict(request.headers), body=body, secret=hook_secret.encode())

    @app.get("/connections/{connection_id}/records")
    def records(
        ctx: Ctx, connection_id: UUID, source_object_type: str, limit: int = 100, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        r = rt()
        r.grants.require(ctx, connection_id, Capability.FILE_PARSER.value) if source_object_type.startswith(
            ("finance.", "knowledge.")
        ) else None
        r.connections.get(ctx, connection_id)
        return [
            _json(x)
            for x in r.records.current_records(
                ctx.tenant_id, connection_id, source_object_type, limit=min(limit, 1000), include_deleted=include_deleted
            )
        ]

    @app.get("/connections/{connection_id}/quarantine")
    def quarantine(ctx: Ctx, connection_id: UUID) -> list[dict[str, Any]]:
        rt().connections.get(ctx, connection_id)
        return [_json(x) for x in rt().records.quarantined(ctx.tenant_id, connection_id)]

    @app.get("/connections/{connection_id}/jobs")
    def jobs(ctx: Ctx, connection_id: UUID) -> list[dict[str, Any]]:
        rt().connections.get(ctx, connection_id)
        return [_json(x) for x in rt().jobs.list(ctx.tenant_id, connection_id)]

    @app.get("/jobs/{job_id}")
    def job(ctx: Ctx, job_id: UUID) -> dict[str, Any]:
        j = rt().jobs.get(ctx.tenant_id, job_id)
        if j is None:
            raise PolicyError("job not found")
        return _json(j)

    @app.post("/jobs/{job_id}/cancel")
    def cancel(ctx: Ctx, job_id: UUID) -> dict[str, Any]:
        return {"cancelled": rt().jobs.cancel(ctx.tenant_id, job_id)}

    @app.post("/jobs/{job_id}/replay")
    def replay(ctx: Ctx, job_id: UUID) -> dict[str, Any]:
        if "admin" not in ctx.roles:
            raise PolicyError("only tenant admins may replay jobs")
        return {"replayed": rt().jobs.replay(ctx.tenant_id, job_id)}

    return app


def _json(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, (bytes, memoryview)):
            out[k] = f"<{len(v)} bytes>"
        else:
            out[k] = v
    return out


__all__ = ["create_app", "DataError", "NotConfiguredError"]
