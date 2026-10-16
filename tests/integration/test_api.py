"""API tests through FastAPI's test client against the real local database (T-02, T-45-style boundaries)."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FIXTURES, TEST_DSN, TEST_KEY
from wayam_connectors.api.app import create_app
from wayam_connectors.platform.config import Settings
from wayam_connectors.platform.runtime import build_runtime
from wayam_connectors.worker.main import DEFAULT_HANDLERS, run_once

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(store, tmp_path_factory):
    settings = Settings(database_url=TEST_DSN, local_secret_key=TEST_KEY, secret_backend="local")
    rt = build_runtime(settings, dsn=TEST_DSN, raw_root=tmp_path_factory.mktemp("raw"))
    return TestClient(create_app(rt)), rt


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_auth_required_and_tenant_boundaries(client, tenants, tenant_a, tenant_b):
    c, rt = client
    assert c.get("/health").status_code == 200
    assert c.get("/connectors").status_code == 401
    assert c.get("/connectors", headers=_h("wck_bogus")).status_code == 401
    admin_a = tenants.issue_api_key(tenant_a.tenant_id, tenant_a.admin.product, ["admin"])
    r = c.post("/connections", headers=_h(admin_a), json={"connector_id": "files.csv", "name": "stmts", "config": {}})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # product key without admin cannot create or grant
    r = c.post("/connections", headers=_h(tenant_a.raw_key), json={"connector_id": "files.csv", "name": "x"})
    assert r.status_code == 403
    # other tenant cannot see it, even by id; same shape as not-found
    assert c.get(f"/connections/{cid}", headers=_h(tenant_b.raw_key)).status_code == 403
    assert c.get("/connections/00000000-0000-0000-0000-000000000000", headers=_h(tenant_a.raw_key)).status_code == 403
    # grant must be a declared capability
    r = c.post(f"/connections/{cid}/grants", headers=_h(admin_a), json={"product": "samyak", "capabilities": ["RecordSync"]})
    assert r.status_code == 405
    r = c.post(f"/connections/{cid}/grants", headers=_h(admin_a), json={"product": "samyak", "capabilities": ["FileParser"]})
    assert r.status_code == 201
    # import through the API as the SAMYAK product
    body = {
        "connector_id": "files.csv",
        "source_ref": "upload://stmt.csv",
        "content_base64": base64.b64encode((FIXTURES / "csv" / "bank_statement_signed.csv").read_bytes()).decode(),
        "parse_options": {
            "mapping": {
                "columns": [
                    {"source": "Txn ID", "target": "id", "required": True},
                    {"source": "Booking Date", "target": "booking", "type": "date", "date_format": "%Y-%m-%d", "required": True},
                    {"source": "Amount", "target": "amount", "type": "decimal", "required": True},
                    {"source": "Currency", "target": "ccy", "required": True},
                ],
                "number_locale": {"decimal_separator": ".", "thousands_separator": ","},
            }
        },
        "domain_mapping": "finance.bank_transaction",
        "mapping_config": {
            "account_id": "ACC",
            "currency": "column:ccy",
            "sign_convention": "signed_amount",
            "amount_column": "amount",
            "booking_date_column": "booking",
            "transaction_id_column": "id",
        },
        "control_totals": {"transaction_count": 4},
    }
    r = c.post(f"/connections/{cid}/imports", headers=_h(tenant_a.raw_key), json=body)
    assert r.status_code == 200, r.text
    assert r.json()["stored"] == 4 and r.json()["summary"]["control_totals"] == "reconciled"
    r = c.post(
        f"/connections/{cid}/imports",
        headers=_h(tenant_a.raw_key),
        json={**body, "control_totals": {"transaction_count": 5}, "allow_duplicate_input": True},
    )
    assert r.status_code == 409 and "transaction_count" in r.json()["details"]["mismatches"]
    r = c.get(f"/connections/{cid}/records", headers=_h(tenant_a.raw_key), params={"source_object_type": "finance.bank_transaction"})
    assert r.status_code == 200 and len(r.json()) == 4 and r.json()[0]["payload"]["amount"]["currency"] == "INR"
    assert (
        c.get(
            f"/connections/{cid}/records", headers=_h(tenant_b.raw_key), params={"source_object_type": "finance.bank_transaction"}
        ).status_code
        == 403
    )
    # a BODHAM key in the same tenant has no grant on this connection
    bodham = tenants.issue_api_key(
        tenant_a.tenant_id, __import__("wayam_connectors.contracts.identity", fromlist=["Product"]).Product.BODHAM
    )
    assert c.post(f"/connections/{cid}/imports", headers=_h(bodham), json=body).status_code == 403
    # revoke blocks further use
    assert c.post(f"/connections/{cid}/revoke", headers=_h(admin_a)).status_code == 200
    assert c.post(f"/connections/{cid}/imports", headers=_h(tenant_a.raw_key), json=body).status_code == 403


def test_secret_never_echoed_and_invalid_config_rejected(client, tenants, tenant_a):
    c, rt = client
    admin = tenants.issue_api_key(tenant_a.tenant_id, tenant_a.admin.product, ["admin"])
    r = c.post(
        "/connections",
        headers=_h(admin),
        json={
            "connector_id": "postgresql.bounded_query",
            "name": "crm",
            "config": {"host": "db.example.com", "database": "crm", "user": "ro"},
            "secret": {"password": "pw"},
        },
    )
    assert r.status_code == 422  # allowed_schemas missing
    r = c.post(
        "/connections",
        headers=_h(admin),
        json={
            "connector_id": "postgresql.bounded_query",
            "name": "crm",
            "config": {"host": "db.example.com", "database": "crm", "user": "ro", "allowed_schemas": ["public"]},
            "secret": {"password": "pw-top-secret"},
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    detail = c.get(f"/connections/{cid}", headers=_h(admin)).json()
    assert "pw-top-secret" not in str(detail) and "secret" not in detail
    assert c.get("/openapi.json").status_code == 200


def test_worker_runs_connection_check_job(client, tenants, tenant_a):
    c, rt = client
    admin = tenants.issue_api_key(tenant_a.tenant_id, tenant_a.admin.product, ["admin"])
    r = c.post(
        "/connections",
        headers=_h(admin),
        json={
            "connector_id": "postgresql.bounded_query",
            "name": "local",
            "config": {
                "host": "127.0.0.1",
                "database": "wayam_connectors_test",
                "user": "wayam",
                "sslmode": "disable",
                "allowed_schemas": ["public"],
                "allow_private_network": True,
            },
            "secret": {"password": "wayam"},
        },
    )
    cid = r.json()["id"]
    from uuid import UUID

    from wayam_connectors.contracts.identity import Product

    jid = rt.jobs.enqueue(tenant_a.tenant_id, UUID(cid), Product.PLATFORM, job_type="connection_check", resource_type="connection")
    assert run_once(rt, DEFAULT_HANDLERS, "test-worker") == "succeeded"
    job = c.get(f"/jobs/{jid}", headers=_h(admin)).json()
    assert job["status"] == "succeeded" and job["result"]["ok"] is False and "read-only role" in job["result"]["detail"]
    assert c.get(f"/connections/{cid}", headers=_h(admin)).json()["status"] == "blocked"
    assert run_once(rt, DEFAULT_HANDLERS, "test-worker") is None
    jid2 = rt.jobs.enqueue(tenant_a.tenant_id, UUID(cid), Product.PLATFORM, job_type="unknown_type", resource_type="x")
    assert run_once(rt, {}, "w") == "failed"
    assert c.get(f"/jobs/{jid2}", headers=_h(admin)).json()["last_error"]["message"].startswith("no handler")


def test_webhook_ingress_verifies_and_stores(client, tenants, tenant_a):
    import hashlib
    import hmac
    import json as _json

    c, rt = client
    admin = tenants.issue_api_key(tenant_a.tenant_id, tenant_a.admin.product, ["admin"])
    r = c.post(
        "/connections",
        headers=_h(admin),
        json={
            "connector_id": "github.repositories",
            "name": "chakram",
            "config": {"repositories": ["mrigankad/Chakram"], "auth_mode": "token"},
            "secret": {"token": "ghp_x", "webhook_secret": "hook"},
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    body = _json.dumps({"zen": "Keep it logically awesome.", "repository": {"id": 1422286600, "full_name": "mrigankad/Chakram"}}).encode()
    sig = "sha256=" + hmac.new(b"hook", body, hashlib.sha256).hexdigest()
    headers = {
        "X-Wayam-Tenant": str(tenant_a.tenant_id),
        "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": "dlv-1",
        "X-GitHub-Event": "ping",
        "Content-Type": "application/json",
    }
    r = c.post(f"/webhooks/{cid}", content=body, headers=headers)
    assert r.status_code == 200 and r.json() == {"status": "received", "delivery_id": "dlv-1", "event_type": "ping", "duplicate": False}
    assert c.post(f"/webhooks/{cid}", content=body, headers=headers).json()["duplicate"] is True
    assert c.post(f"/webhooks/{cid}", content=body + b"x", headers=headers).status_code == 401  # tampered body
    assert (
        c.post(f"/webhooks/{cid}", content=body, headers={**headers, "X-Wayam-Tenant": str(__import__("uuid").uuid4())}).status_code == 403
    )
    assert c.post(f"/webhooks/{cid}", content=body, headers={k: v for k, v in headers.items() if k != "X-Wayam-Tenant"}).status_code == 401
    pending = rt.webhooks.pending(tenant_a.tenant_id, __import__("uuid").UUID(cid))
    assert len(pending) == 1 and pending[0]["event_type"] == "ping"
