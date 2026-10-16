"""PostgreSQL access with tenant isolation enforced in the database.

Every tenant-scoped statement runs inside `tenant_tx(tenant_id)`, which sets the
`wayam.tenant_id` GUC for the transaction; row-level security policies on every
tenant table compare `tenant_id` to it. Platform-level work that must see across
tenants (queue claim, migrations) uses `bypass_tx()` which sets `wayam.bypass`
and is only ever called from trusted runtime code, never from a request handler.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import re
from collections.abc import Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class Store:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def connect(self) -> psycopg.Connection[dict]:  # type: ignore[type-arg]
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    @contextlib.contextmanager
    def tenant_tx(self, tenant_id: UUID) -> Iterator[psycopg.Cursor[dict]]:  # type: ignore[type-arg]
        """A transaction limited to one tenant by RLS. Commits on success, rolls back on error."""
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('wayam.tenant_id', %s, true)", (str(tenant_id),))
                cur.execute("SELECT set_config('wayam.bypass', '', true)")
                yield cur
            conn.commit()

    @contextlib.contextmanager
    def bypass_tx(self, reason: str) -> Iterator[psycopg.Cursor[dict]]:  # type: ignore[type-arg]
        """Cross-tenant transaction for runtime internals. `reason` is recorded on the session."""
        if reason not in {"worker_claim", "migration", "auth", "admin", "test"}:
            raise ValueError(f"unknown bypass reason {reason!r}")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('wayam.bypass', %s, true)", (reason,))
                cur.execute("SELECT set_config('wayam.tenant_id', '', true)")
                yield cur
            conn.commit()

    # ------------------------------------------------------------ migrations

    def migrate(self) -> list[str]:
        """Apply unapplied SQL migrations in order. Returns the names applied."""
        files = sorted(
            (p.name, p.read_text(encoding="utf-8"))
            for p in importlib.resources.files("wayam_connectors.platform.migrations").iterdir()
            if MIGRATION_RE.match(p.name)
        )
        applied: list[str] = []
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migration (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                )
                cur.execute("SELECT name FROM schema_migration")
                done = {r["name"] for r in cur.fetchall()}
                for name, sql in files:
                    if name in done:
                        continue
                    cur.execute(sql)  # type: ignore[arg-type]
                    cur.execute("INSERT INTO schema_migration (name) VALUES (%s)", (name,))
                    applied.append(name)
            conn.commit()
        return applied

    def reset_for_tests(self) -> None:
        """Drop everything. Only used by the test suite against a database named *_test."""
        if not self.dsn.rstrip("/").endswith("_test"):
            raise RuntimeError("reset_for_tests only runs against a *_test database")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            conn.commit()
