"""Integration tests against a real local PostgreSQL: RLS isolation, credentials, jobs, commit boundary, webhooks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import uuid
from datetime import UTC, datetime

import pytest

from tests.conftest import TEST_KEY
from wayam_connectors.contracts.capabilities import WebhookDelivery, WebhookVerdict
from wayam_connectors.contracts.envelope import Operation, SourceEnvelope, canonical_checksum
from wayam_connectors.contracts.errors import AuthError, PolicyError, RateLimitedError, RetryableError, ValidationError
from wayam_connectors.contracts.identity import Product
from wayam_connectors.platform.connections import ConnectionService
from wayam_connectors.platform.credentials import CredentialStore, LocalSecretBackend
from wayam_connectors.platform.jobs import JobQueue
from wayam_connectors.platform.records import QuarantineItem, RecordStore
from wayam_connectors.platform.webhooks import WebhookInbox

pytestmark = pytest.mark.integration


def _env(t, conn, oid="obj-1", version="v1", payload=None, op=Operation.UPSERT):
    payload = payload or {"k": oid}
    return SourceEnvelope(
        tenant_id=t.tenant_id,
        product="samyak",
        connection_id=conn,
        source_system="files.csv",
        source_object_id=oid,
        source_object_type="finance.bank_transaction",
        schema_version="finance/1",
        source_version=version,
        operation=op,
        observed_at=datetime.now(UTC),
        checksum=canonical_checksum(payload),
        provenance=f"f#{oid}",
        payload=payload,
    )


def _connection(store, t, name="c"):
    return ConnectionService(store).create(t.admin, "files.csv", name, {})


# ---------------------------------------------------------------- T-01 / T-02 / T-03 identity


def test_two_tenants_same_source_ids_never_cross(store, tenant_a, tenant_b, grants):
    ca, cb = _connection(store, tenant_a), _connection(store, tenant_b)
    rs = RecordStore(store)
    rs.commit_page(
        tenant_a.tenant_id,
        ca,
        resource_type="r",
        resource_id="x",
        envelopes=[_env(tenant_a, ca, payload={"owner": "a"})],
        next_cursor={"page": 1},
        cursor_version="1",
    )
    rs.commit_page(
        tenant_b.tenant_id,
        cb,
        resource_type="r",
        resource_id="x",
        envelopes=[_env(tenant_b, cb, payload={"owner": "b"})],
        next_cursor={"page": 9},
        cursor_version="1",
    )
    a = rs.current_records(tenant_a.tenant_id, ca, "finance.bank_transaction")
    b = rs.current_records(tenant_b.tenant_id, cb, "finance.bank_transaction")
    assert [r["payload"]["owner"] for r in a] == ["a"] and [r["payload"]["owner"] for r in b] == ["b"]
    # same connection id, wrong tenant: nothing (RLS), not even by guessing the id
    assert rs.current_records(tenant_b.tenant_id, ca, "finance.bank_transaction") == []
    assert rs.get_checkpoint(tenant_b.tenant_id, ca, "r", "x") is None
    assert rs.get_checkpoint(tenant_a.tenant_id, ca, "r", "x")["cursor"] == {"page": 1}
    # raw SQL with the wrong tenant set sees nothing even without a WHERE tenant_id clause
    with store.tenant_tx(tenant_b.tenant_id) as cur:
        cur.execute("SELECT count(*) AS n FROM normalized_record WHERE connection_id=%s", (ca,))
        assert cur.fetchone()["n"] == 0
    # no tenant set at all: nothing
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM normalized_record")
        assert cur.fetchone()["n"] == 0
    # cannot insert a row for another tenant
    with pytest.raises(Exception):  # noqa: B017 - RLS WITH CHECK violation surfaces as psycopg error
        with store.tenant_tx(tenant_b.tenant_id) as cur:
            cur.execute("INSERT INTO audit_event (tenant_id, actor, action) VALUES (%s, 'x', 'y')", (tenant_a.tenant_id,))


def test_guessed_connection_id_and_removed_grant(store, tenant_a, tenant_b, grants):
    ca = _connection(store, tenant_a)
    grants.grant(tenant_a.admin, ca, Product.SAMYAK, ["FileParser"])
    assert grants.require(tenant_a.product_ctx, ca, "FileParser")["resource_allowlist"] == []
    with pytest.raises(PolicyError) as e1:
        grants.require(tenant_b.product_ctx, ca, "FileParser")  # other tenant guessing the id
    with pytest.raises(PolicyError) as e2:
        grants.require(tenant_a.product_ctx, uuid.uuid4(), "FileParser")  # nonexistent
    assert str(e1.value) == str(e2.value)  # no existence disclosure
    with pytest.raises(PolicyError):
        grants.require(tenant_a.product_ctx, ca, "RecordSync")  # not granted capability
    with pytest.raises(PolicyError):
        ConnectionService(store).get(tenant_b.admin, ca)
    assert grants.revoke(tenant_a.admin, ca, Product.SAMYAK) == 1
    with pytest.raises(PolicyError):
        grants.require(tenant_a.product_ctx, ca, "FileParser")
    with pytest.raises(PolicyError):
        grants.grant(tenant_a.product_ctx, ca, Product.SAMYAK, ["FileParser"])  # non-admin cannot grant


def test_api_key_auth(store, tenants, tenant_a):
    with pytest.raises(AuthError):
        tenants.authenticate(None)
    with pytest.raises(AuthError):
        tenants.authenticate("wck_not-a-real-key")
    ctx = tenants.authenticate(tenant_a.raw_key)
    assert ctx.tenant_id == tenant_a.tenant_id and ctx.product == Product.SAMYAK
    assert tenants.revoke_api_key(tenant_a.tenant_id, tenant_a.raw_key[:8]) == 1
    with pytest.raises(AuthError):
        tenants.authenticate(tenant_a.raw_key)


# ---------------------------------------------------------------- credentials


def test_credentials_bound_to_tenant_and_connection(store, tenant_a, tenant_b, credentials):
    ca, cb = _connection(store, tenant_a), _connection(store, tenant_b)
    credentials.put(tenant_a.tenant_id, ca, {"token": "secret-1"})
    assert credentials.get(tenant_a.tenant_id, ca) == {"token": "secret-1"}
    with pytest.raises(AuthError):
        credentials.get(tenant_b.tenant_id, ca)
    # rotation: new version replaces, old revoked
    credentials.put(tenant_a.tenant_id, ca, {"token": "secret-2"})
    assert credentials.get(tenant_a.tenant_id, ca)["token"] == "secret-2"
    with store.tenant_tx(tenant_a.tenant_id) as cur:
        cur.execute(
            "SELECT version, revoked_at IS NULL AS live, ciphertext FROM credential_ref WHERE connection_id=%s ORDER BY version", (ca,)
        )
        rows = cur.fetchall()
    assert [(r["version"], r["live"]) for r in rows] == [(1, False), (2, True)]
    assert b"secret" not in bytes(rows[1]["ciphertext"])
    # ciphertext moved to another connection cannot be decrypted (AAD binding)
    with store.bypass_tx("test") as cur:
        cur.execute(
            "INSERT INTO credential_ref (tenant_id, connection_id, backend, key_id, ciphertext, version) "
            "VALUES (%s,%s,'local','local-1',%s,1)",
            (tenant_b.tenant_id, cb, bytes(rows[1]["ciphertext"])),
        )
    with pytest.raises(AuthError):
        credentials.get(tenant_b.tenant_id, cb)
    # wrong key
    other = CredentialStore(store, LocalSecretBackend(base64.b64encode(b"\x02" * 32).decode()))
    with pytest.raises(AuthError):
        other.get(tenant_a.tenant_id, ca)
    credentials.revoke(tenant_a.tenant_id, ca, "user revoked consent")
    with pytest.raises(AuthError):
        credentials.get(tenant_a.tenant_id, ca)
    assert ConnectionService(store).get(tenant_a.admin, ca)["status"] == "revoked"


def test_local_backend_requires_key():
    from wayam_connectors.contracts.errors import NotConfiguredError

    with pytest.raises(NotConfiguredError):
        LocalSecretBackend(None)
    with pytest.raises(NotConfiguredError):
        LocalSecretBackend(base64.b64encode(b"short").decode())
    assert LocalSecretBackend(TEST_KEY).name == "local"


# ---------------------------------------------------------------- jobs: lease, retry, dead letter, cancel, replay


def test_job_lifecycle(store, tenant_a, tenant_b):
    ca, cb = _connection(store, tenant_a), _connection(store, tenant_b)
    q = JobQueue(store, lease_seconds=60, base_backoff=0.01, max_backoff=0.02)
    j1 = q.enqueue(tenant_a.tenant_id, ca, Product.SAMYAK, job_type="sync", resource_type="rows", max_attempts=2)
    with pytest.raises(ValidationError):
        q.enqueue(tenant_a.tenant_id, ca, Product.SAMYAK, job_type="sync", resource_type="rows")  # partition already active
    j2 = q.enqueue(tenant_b.tenant_id, cb, Product.BODHAM, job_type="sync", resource_type="rows")
    assert q.get(tenant_b.tenant_id, j1) is None  # other tenant cannot see it

    leased = q.claim("w1", tenant_id=tenant_a.tenant_id)
    assert leased is not None and leased.id == j1 and leased.attempts == 1
    assert leased.grant(["RecordSync"]).tenant_id == tenant_a.tenant_id
    assert q.heartbeat(leased)
    # retryable failure -> queued with backoff; second failure -> dead letter at max_attempts
    assert q.fail(leased, RetryableError("boom 500")) == "queued"
    assert not q.heartbeat(leased)  # lease is gone
    assert q.get(tenant_a.tenant_id, j1)["status"] == "queued"
    import time

    time.sleep(0.05)
    claimed = {q.claim("w2", tenant_id=tenant_a.tenant_id).id, q.claim("w2", tenant_id=tenant_b.tenant_id).id}
    assert claimed == {j1, j2}
    assert q.claim("w3", tenant_id=tenant_a.tenant_id) is None and q.claim("w3", tenant_id=tenant_b.tenant_id) is None
    leased_again = next(j for j in [q.get(tenant_a.tenant_id, j1)] if j)
    assert leased_again["attempts"] == 2
    # rebuild LeasedJob for j1 via the queue table lease token
    with store.tenant_tx(tenant_a.tenant_id) as cur:
        cur.execute("SELECT lease_token, leased_until FROM sync_job WHERE id=%s", (j1,))
        row = cur.fetchone()
    from wayam_connectors.platform.jobs import LeasedJob

    lj = LeasedJob(
        id=j1,
        tenant_id=tenant_a.tenant_id,
        connection_id=ca,
        product=Product.SAMYAK,
        job_type="sync",
        resource_type="rows",
        resource_id=None,
        payload={},
        attempts=2,
        max_attempts=2,
        lease_token=row["lease_token"],
        leased_until=row["leased_until"],
    )
    assert q.fail(lj, RateLimitedError("429", retry_after_seconds=0.01)) == "dead_letter"
    st = q.get(tenant_a.tenant_id, j1)
    assert st["status"] == "dead_letter" and st["last_error"]["class"] == "rate_limited"
    assert q.replay(tenant_a.tenant_id, j1) and q.get(tenant_a.tenant_id, j1)["attempts"] == 0
    assert q.cancel(tenant_a.tenant_id, j1) and q.get(tenant_a.tenant_id, j1)["status"] == "cancelled"
    assert not q.replay(tenant_b.tenant_id, j1)  # cross-tenant replay does nothing


def test_auth_failure_blocks_connection_without_retry(store, tenant_a):
    ca = _connection(store, tenant_a)
    q = JobQueue(store)
    jid = q.enqueue(tenant_a.tenant_id, ca, Product.SAMYAK, job_type="sync", resource_type="t")
    leased = q.claim("w", tenant_id=tenant_a.tenant_id)
    assert leased.id == jid
    assert q.fail(leased, AuthError("token revoked", details={"token": "ghp_shouldnotappear1234567890abcdefgh"})) == "failed"
    st = q.get(tenant_a.tenant_id, jid)
    assert st["status"] == "failed" and "ghp_" not in json.dumps(st["last_error"])
    assert ConnectionService(store).get(tenant_a.admin, ca)["status"] == "blocked"


def test_expired_lease_is_reclaimed_and_concurrent_claims_are_exclusive(store, tenant_a):
    ca = _connection(store, tenant_a)
    q = JobQueue(store, lease_seconds=0)
    jid = q.enqueue(tenant_a.tenant_id, ca, Product.SAMYAK, job_type="sync", resource_type="t", max_attempts=10)
    first = q.claim("crashed-worker", tenant_id=tenant_a.tenant_id)
    assert first.id == jid
    second = q.claim("new-worker", tenant_id=tenant_a.tenant_id)  # lease already expired (0s) -> reclaimed by another worker
    assert second is not None and second.id == jid and second.lease_token != first.lease_token
    assert not q.complete(first, {})  # the crashed worker's token no longer works
    assert q.complete(second, {"ok": True, "token": "x"})
    assert q.get(tenant_a.tenant_id, jid)["result"] == {"ok": True, "token": "[REDACTED]"}

    # many workers racing for many jobs: each job claimed exactly once
    ids = [q.enqueue(tenant_a.tenant_id, ca, Product.SAMYAK, job_type="race", resource_type="t", resource_id=str(i)) for i in range(20)]
    q2 = JobQueue(store, lease_seconds=60)
    got: list[uuid.UUID] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            j = q2.claim(name, tenant_id=tenant_a.tenant_id)
            if j is None:
                return
            with lock:
                got.append(j.id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(ids)


# ---------------------------------------------------------------- commit boundary (T-13, T-16, T-17)


def test_commit_is_atomic_and_idempotent(store, tenant_a):
    ca = _connection(store, tenant_a)
    rs = RecordStore(store)
    envs = [_env(tenant_a, ca, "o1"), _env(tenant_a, ca, "o2")]
    s1 = rs.commit_page(
        tenant_a.tenant_id, ca, resource_type="r", resource_id=None, envelopes=envs, next_cursor={"p": 1}, cursor_version="1"
    )
    assert (s1.inserted, s1.replayed, s1.outbox_events) == (2, 0, 1)
    s2 = rs.commit_page(
        tenant_a.tenant_id, ca, resource_type="r", resource_id=None, envelopes=envs, next_cursor={"p": 1}, cursor_version="1"
    )
    assert (s2.inserted, s2.replayed, s2.outbox_events) == (0, 2, 0)  # replay after a crash-before-ack: counted, not duplicated
    # new version supersedes, delete marks
    rs.commit_page(
        tenant_a.tenant_id,
        ca,
        resource_type="r",
        resource_id=None,
        envelopes=[_env(tenant_a, ca, "o1", "v2", {"k": "o1", "n": 2})],
        next_cursor={"p": 2},
        cursor_version="1",
    )
    cur = rs.current_records(tenant_a.tenant_id, ca, "finance.bank_transaction")
    assert {r["source_object_id"]: r["source_version"] for r in cur} == {"o1": "v2", "o2": "v1"}
    rs.commit_page(
        tenant_a.tenant_id,
        ca,
        resource_type="r",
        resource_id=None,
        envelopes=[_env(tenant_a, ca, "o2", "v3", op=Operation.DELETE)],
        next_cursor={"p": 3},
        cursor_version="1",
    )
    assert [r["source_object_id"] for r in rs.current_records(tenant_a.tenant_id, ca, "finance.bank_transaction")] == ["o1"]
    assert len(rs.current_records(tenant_a.tenant_id, ca, "finance.bank_transaction", include_deleted=True)) == 2
    # a bad envelope in a batch rolls back the whole page (crash before commit == nothing persisted)
    wrong_conn = _env(tenant_a, uuid.uuid4(), "o9")
    with pytest.raises(Exception):  # noqa: B017
        rs.commit_page(
            tenant_a.tenant_id,
            ca,
            resource_type="r",
            resource_id=None,
            envelopes=[_env(tenant_a, ca, "o8"), wrong_conn],
            next_cursor={"p": 4},
            cursor_version="1",
        )
    assert rs.get_checkpoint(tenant_a.tenant_id, ca, "r", None)["cursor"] == {"p": 3}
    assert not any(r["source_object_id"] == "o8" for r in rs.current_records(tenant_a.tenant_id, ca, "finance.bank_transaction"))
    # quarantine rows are stored with the page
    s = rs.commit_page(
        tenant_a.tenant_id,
        ca,
        resource_type="r",
        resource_id=None,
        envelopes=[],
        quarantine=[QuarantineItem(provenance="f#row=3", reason="mapping", error={"message": "bad"})],
        next_cursor={"p": 5},
        cursor_version="1",
    )
    assert s.quarantined == 1 and rs.quarantined(tenant_a.tenant_id, ca)[0]["reason"] == "mapping"
    pending = rs.pending_outbox(tenant_a.tenant_id)
    assert [p["topic"] for p in pending] == ["records.committed"] * 4
    rs.mark_published(tenant_a.tenant_id, [p["id"] for p in pending])
    assert rs.pending_outbox(tenant_a.tenant_id) == []


# ---------------------------------------------------------------- webhook inbox (T-24, T-25)


class HmacReceiver:
    """Minimal receiver used to test the inbox: HMAC-SHA256 over raw bytes in X-Signature, id in X-Delivery."""

    CONNECTOR_ID = "test.hook"

    def verify_delivery(self, delivery: WebhookDelivery, secret: bytes) -> WebhookVerdict:
        expected = hmac.new(secret, delivery.body, hashlib.sha256).hexdigest()
        given = delivery.headers.get("X-Signature", "")
        if not hmac.compare_digest(expected, given):
            return WebhookVerdict(verified=False, delivery_id=None, event_type=None, reason="signature mismatch")
        return WebhookVerdict(verified=True, delivery_id=delivery.headers.get("X-Delivery"), event_type=delivery.headers.get("X-Event"))

    def envelopes_from_delivery(self, delivery: WebhookDelivery):
        return []


def test_webhook_inbox_verifies_dedups_and_redacts(store, tenant_a):
    ca = _connection(store, tenant_a)
    inbox = WebhookInbox(store)
    secret = b"s3cret"
    body = b'{"action":"opened"}'
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    headers = {"X-Signature": sig, "X-Delivery": "d-1", "X-Event": "pull_request", "Authorization": "Bearer abc"}
    r1 = inbox.receive(tenant_a.tenant_id, ca, HmacReceiver(), headers=headers, body=body, secret=secret)
    assert r1 == {"status": "received", "delivery_id": "d-1", "event_type": "pull_request", "duplicate": False}
    r2 = inbox.receive(tenant_a.tenant_id, ca, HmacReceiver(), headers=headers, body=body, secret=secret)
    assert r2["duplicate"] is True
    with pytest.raises(AuthError):
        inbox.receive(tenant_a.tenant_id, ca, HmacReceiver(), headers=headers, body=body + b" ", secret=secret)  # body modified
    with pytest.raises(AuthError):
        inbox.receive(tenant_a.tenant_id, ca, HmacReceiver(), headers={**headers, "X-Signature": "00"}, body=body, secret=secret)
    with pytest.raises(ValidationError):
        inbox.receive(
            tenant_a.tenant_id,
            ca,
            HmacReceiver(),
            headers={k: v for k, v in headers.items() if k != "X-Delivery"},
            body=body,
            secret=secret,
        )
    pending = inbox.pending(tenant_a.tenant_id, ca)
    assert len(pending) == 1 and pending[0]["headers"]["Authorization"] == "[REDACTED]" and bytes(pending[0]["body"]) == body
    inbox.mark(tenant_a.tenant_id, pending[0]["id"], "processed")
    assert inbox.pending(tenant_a.tenant_id, ca) == []
