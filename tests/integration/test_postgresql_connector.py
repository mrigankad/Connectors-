"""Local-dependency integration tests for postgresql.bounded_query (T-41, T-42, T-43)."""

from __future__ import annotations

import psycopg
import pytest

from tests.conftest import TEST_DSN
from wayam_connectors.connectors.postgresql.bounded_query.adapter import PostgresBoundedQuery
from wayam_connectors.contracts.capabilities import QueryRequest
from wayam_connectors.contracts.errors import AuthError, PolicyError, RetryableError, UnsupportedOperationError, ValidationError

pytestmark = pytest.mark.integration

RO_ROLE, RO_PASSWORD = "wayam_ro_test", "ro-secret"


@pytest.fixture(scope="module")
def source_db(store):
    """A customer-like schema with a read-only role and an RLS table keyed by a GUC."""
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS crm CASCADE")
        conn.execute("CREATE SCHEMA crm")
        conn.execute("CREATE TABLE crm.accounts (id int PRIMARY KEY, name text NOT NULL, owner text NOT NULL, balance numeric(12,2))")
        conn.execute(
            "INSERT INTO crm.accounts SELECT i, 'acct '||i, CASE WHEN i % 2 = 0 THEN 'alice' ELSE 'bob' END, i * 1.5 "
            "FROM generate_series(1, 50) i"
        )
        conn.execute("CREATE VIEW crm.big_accounts AS SELECT * FROM crm.accounts WHERE balance > 50")
        conn.execute("ALTER TABLE crm.accounts ENABLE ROW LEVEL SECURITY")
        conn.execute("ALTER TABLE crm.accounts FORCE ROW LEVEL SECURITY")
        conn.execute("CREATE POLICY per_owner ON crm.accounts USING (owner = current_setting('app.current_user', true))")
        conn.execute(f"DROP ROLE IF EXISTS {RO_ROLE}")
        conn.execute(f"CREATE ROLE {RO_ROLE} LOGIN PASSWORD '{RO_PASSWORD}'")
        conn.execute(f"GRANT USAGE ON SCHEMA crm TO {RO_ROLE}")
        conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA crm TO {RO_ROLE}")
    yield
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS crm CASCADE")
        conn.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{RO_ROLE}') THEN "
            f"EXECUTE 'GRANT {RO_ROLE} TO wayam'; EXECUTE 'REVOKE ALL ON DATABASE wayam_connectors_test FROM {RO_ROLE}'; "
            f"EXECUTE 'DROP OWNED BY {RO_ROLE}'; "
            f"EXECUTE 'DROP ROLE {RO_ROLE}'; END IF; END $$"
        )


def _cfg(**over):
    base = {
        "host": "127.0.0.1",
        "port": 5432,
        "database": "wayam_connectors_test",
        "user": RO_ROLE,
        "sslmode": "disable",
        "allowed_schemas": ["crm"],
        "max_rows": 20,
        "statement_timeout_ms": 2000,
        "user_context_setting": "app.current_user",
        "allow_private_network": True,
    }
    base.update(over)
    return base


def test_private_host_needs_gateway_flag():
    with pytest.raises(PolicyError):
        PostgresBoundedQuery(_cfg(allow_private_network=False), {"password": "x"})


async def test_check_connection_verifies_read_only_role(source_db):
    ok = await PostgresBoundedQuery(_cfg(), {"password": RO_PASSWORD}).check_connection()
    assert ok.ok and ok.identity == RO_ROLE and "PostgreSQL" in (ok.edition or "")
    elevated = await PostgresBoundedQuery(_cfg(user="wayam"), {"password": "wayam"}).check_connection()
    assert not elevated.ok and "read-only role" in elevated.detail
    with pytest.raises(AuthError):
        await PostgresBoundedQuery(_cfg(), {"password": "wrong"}).check_connection()
    missing = await PostgresBoundedQuery(_cfg(allowed_schemas=["crm", "nope"]), {"password": RO_PASSWORD}).check_connection()
    assert not missing.ok and "missing" in missing.detail
    with pytest.raises(RetryableError):
        await PostgresBoundedQuery(_cfg(port=5999, connect_timeout_s=1), {"password": RO_PASSWORD}).check_connection()


async def test_discover_allowlisted_schema_only(source_db):
    a = PostgresBoundedQuery(_cfg(), {"password": RO_PASSWORD})
    found = [r async for r in a.discover()]
    assert {(r.resource_type, r.resource_id) for r in found} == {("table", "crm.accounts"), ("view", "crm.big_accounts")}
    acc = next(r for r in found if r.resource_id == "crm.accounts")
    assert acc.extra["row_level_security"] is True
    assert [c["name"] for c in acc.schema_["columns"]] == ["id", "name", "owner", "balance"]
    assert [r.resource_id for r in [x async for x in a.discover("view")]] == ["crm.big_accounts"]
    with pytest.raises(UnsupportedOperationError):
        _ = [x async for x in a.discover("sequence")]


async def test_bounded_query_limits_params_rls_and_writes(source_db):
    a = PostgresBoundedQuery(_cfg(), {"password": RO_PASSWORD})
    # T-43: source RLS via user context; no context -> policy sees NULL -> no rows
    none = await a.query(QueryRequest(sql="SELECT id FROM crm.accounts ORDER BY id"))
    assert none.row_count == 0
    alice = await a.query(
        QueryRequest(
            sql="SELECT id, balance FROM crm.accounts WHERE id > %(min)s ORDER BY id", params={"min": 10}, user_context="alice", max_rows=5
        )
    )
    assert alice.columns == ["id", "balance"] and [r[0] for r in alice.rows] == [12, 14, 16, 18, 20] and alice.truncated
    bob = await a.query(QueryRequest(sql="SELECT count(*) FROM crm.accounts", user_context="bob"))
    assert bob.rows == [[25]] and not bob.truncated
    # cap from config
    with pytest.raises(PolicyError):
        await a.query(QueryRequest(sql="SELECT 1", max_rows=21))
    # T-41: writes and multi-statements rejected before reaching the DB
    for bad in ["DELETE FROM crm.accounts", "SELECT 1; DELETE FROM crm.accounts", "SELECT * FROM crm.accounts FOR UPDATE"]:
        with pytest.raises(PolicyError):
            await a.query(QueryRequest(sql=bad, user_context="alice"))
    # a function with side effects that slips past the static guard still fails: READ ONLY transaction
    with pytest.raises(PolicyError, match="read-only"):
        await a.query(QueryRequest(sql="SELECT id FROM crm.accounts FOR SHARE", user_context="alice"))
    # T-42: statement timeout
    with pytest.raises(RetryableError, match="timeout"):
        await a.query(QueryRequest(sql="SELECT count(*) FROM generate_series(1, 200000000) g", timeout_seconds=0.2))
    # search_path is pg_catalog: unqualified names do not resolve
    with pytest.raises(ValidationError):
        await a.query(QueryRequest(sql="SELECT * FROM accounts", user_context="alice"))
    with pytest.raises(ValidationError):
        await a.query(QueryRequest(sql="SELECT nope FROM crm.accounts", user_context="alice"))
    # user context without a configured setting is unsupported, not ignored
    with pytest.raises(UnsupportedOperationError):
        await PostgresBoundedQuery(_cfg(user_context_setting=None), {"password": RO_PASSWORD}).query(
            QueryRequest(sql="SELECT 1", user_context="alice")
        )
    # nothing was written and the source is untouched
    with psycopg.connect(TEST_DSN) as conn:
        total = 0
        for who in ("alice", "bob"):
            conn.execute("SELECT set_config('app.current_user', %s, false)", (who,))
            total += conn.execute("SELECT count(*) FROM crm.accounts").fetchone()[0]
        assert total == 50
