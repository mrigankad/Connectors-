"""PostgreSQL bounded read connector.

Edition targeted: PostgreSQL 13+ wire protocol via psycopg 3 (tested locally on 16).
Capabilities: ConnectionCheck, ResourceDiscovery (allowlisted schemas), BoundedQuery.

Every query runs in a READ ONLY transaction with a statement_timeout, as the role the
connection was configured with (which must itself be read-only: the adapter checks and
refuses roles that can write to the allowlisted schemas), with parameters passed
server-side, and wrapped in a LIMIT so the database never streams more than the cap.
Per-user data policy is expressed as a source-side session variable (`user_context`
-> `SET LOCAL <config.user_context_setting> = ...`) that the customer's RLS policies read.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import tuple_row
from pydantic import BaseModel, ConfigDict, Field, field_validator

from wayam_connectors.contracts.capabilities import CheckResult, QueryRequest, QueryResult, Resource
from wayam_connectors.contracts.errors import (
    AuthError,
    PolicyError,
    RetryableError,
    ScopeError,
    UnsupportedOperationError,
    ValidationError,
)
from wayam_connectors.contracts.identity import AdapterContext

from .sql_guard import guard_read_only, wrap_with_limit

ADAPTER_VERSION = "0.1.0"
CONNECTOR_ID = "postgresql.bounded_query"
_IDENT = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"


class PostgresConfig(BaseModel):
    """Non-secret connection configuration (validated against manifest.config_schema)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    host: str
    port: int = 5432
    database: str
    user: str
    sslmode: str = Field(default="require", pattern=r"^(require|verify-ca|verify-full|disable)$")
    allowed_schemas: list[str] = Field(min_length=1)
    max_rows: int = Field(default=1000, ge=1, le=100_000)
    statement_timeout_ms: int = Field(default=10_000, ge=100, le=600_000)
    connect_timeout_s: int = Field(default=5, ge=1, le=60)
    user_context_setting: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    allow_private_network: bool = Field(default=False, description="Only through a customer gateway deployment")

    @field_validator("allowed_schemas")
    @classmethod
    def _idents(cls, v: list[str]) -> list[str]:
        import re

        for s in v:
            if not re.match(_IDENT, s):
                raise ValueError(f"invalid schema name {s!r}")
        return v


class PostgresSecret(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    password: str


class PostgresBoundedQuery:
    CONNECTOR_ID = CONNECTOR_ID
    ADAPTER_VERSION = ADAPTER_VERSION

    def __init__(self, config: Mapping[str, Any], secret: Mapping[str, Any], context: AdapterContext | None = None) -> None:
        self.context = context
        self.config = PostgresConfig.model_validate(dict(config))
        self.secret = PostgresSecret.model_validate(dict(secret))
        if not self.config.allow_private_network:
            from wayam_connectors.platform.security.ssrf import _reject_private_host

            _reject_private_host(self.config.host, resolve=False)

    # ------------------------------------------------------------ internals

    def _connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        c = self.config
        try:
            conn = psycopg.connect(
                host=c.host,
                port=c.port,
                dbname=c.database,
                user=c.user,
                password=self.secret.password,
                sslmode=c.sslmode,
                connect_timeout=c.connect_timeout_s,
                application_name="wayam-connectors",
                row_factory=tuple_row,
                autocommit=False,
            )
        except psycopg.OperationalError as exc:
            msg = str(exc).lower()
            if "password authentication failed" in msg or "no pg_hba" in msg or "role" in msg and "does not exist" in msg:
                raise AuthError("database rejected the credentials") from exc
            raise RetryableError("cannot reach database", details={"error": _safe(exc)}) from exc
        return conn

    def _begin_read_only(self, cur: psycopg.Cursor[Any], timeout_ms: int, user_context: str | None) -> None:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(pgsql.SQL("SET LOCAL statement_timeout = {}").format(pgsql.Literal(int(timeout_ms))))
        cur.execute("SET LOCAL search_path = pg_catalog")
        if user_context is not None:
            if not self.config.user_context_setting:
                raise UnsupportedOperationError("this connection has no user_context_setting configured; per-user policy unavailable")
            cur.execute("SELECT set_config(%s, %s, true)", (self.config.user_context_setting, user_context))

    def _check_role_is_read_only(self, cur: psycopg.Cursor[Any]) -> list[str]:
        """Refuse to operate with a role that could write to the allowlisted schemas."""
        cur.execute(
            """
            SELECT n.nspname || '.' || c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p','v','m','f')
              AND (has_table_privilege(current_user, c.oid, 'INSERT')
                   OR has_table_privilege(current_user, c.oid, 'UPDATE')
                   OR has_table_privilege(current_user, c.oid, 'DELETE')
                   OR has_table_privilege(current_user, c.oid, 'TRUNCATE'))
            ORDER BY 1 LIMIT 20
            """,
            (self.config.allowed_schemas,),
        )
        return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------ capabilities

    async def check_connection(self) -> CheckResult:
        return await asyncio.to_thread(self._check_connection_sync)

    def _check_connection_sync(self) -> CheckResult:
        with self._connect() as conn, conn.cursor() as cur:
            self._begin_read_only(cur, self.config.statement_timeout_ms, None)
            cur.execute("SELECT current_user, version(), current_setting('server_version_num')")
            user, version, num = cur.fetchone()  # type: ignore[misc]
            cur.execute("SELECT rolsuper OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname = current_user")
            elevated = bool(cur.fetchone()[0])  # type: ignore[index]
            writable = self._check_role_is_read_only(cur)
            cur.execute("SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)", (self.config.allowed_schemas,))
            present = {r[0] for r in cur.fetchall()}
            conn.rollback()
        missing = sorted(set(self.config.allowed_schemas) - present)
        if int(num) < 130000:
            return CheckResult(ok=False, identity=user, edition=version, detail="PostgreSQL 13+ required", checked_at=datetime.now(UTC))
        if elevated:
            return CheckResult(
                ok=False,
                identity=user,
                edition=version,
                detail="role has superuser/createrole/createdb; use a read-only role",
                checked_at=datetime.now(UTC),
            )
        if writable:
            return CheckResult(
                ok=False,
                identity=user,
                edition=version,
                detail=f"role can write to allowlisted objects: {writable[:5]}",
                checked_at=datetime.now(UTC),
            )
        if missing:
            return CheckResult(
                ok=False, identity=user, edition=version, detail=f"allowlisted schemas missing: {missing}", checked_at=datetime.now(UTC)
            )
        return CheckResult(ok=True, identity=user, edition=version, detail="read-only role verified", checked_at=datetime.now(UTC))

    async def discover(self, resource_type: str | None = None) -> AsyncIterator[Resource]:
        if resource_type not in (None, "table", "view"):
            raise UnsupportedOperationError("discover supports resource_type table|view", details={"given": resource_type})
        for r in await asyncio.to_thread(self._discover_sync, resource_type):
            yield r

    def _discover_sync(self, resource_type: str | None) -> list[Resource]:
        kinds = {"table": ("r", "p", "f"), "view": ("v", "m")}
        wanted = kinds[resource_type] if resource_type else kinds["table"] + kinds["view"]
        with self._connect() as conn, conn.cursor() as cur:
            self._begin_read_only(cur, self.config.statement_timeout_ms, None)
            cur.execute(
                """
                SELECT n.nspname, c.relname, c.relkind,
                       (SELECT json_agg(json_build_object('name', a.attname, 'type', pg_catalog.format_type(a.atttypid, a.atttypmod),
                                                          'nullable', NOT a.attnotnull) ORDER BY a.attnum)
                        FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped) AS cols,
                       c.relrowsecurity
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = ANY(%s) AND c.relkind = ANY(%s)
                  AND has_table_privilege(current_user, c.oid, 'SELECT')
                ORDER BY 1, 2
                """,
                (self.config.allowed_schemas, list(wanted)),
            )
            rows = cur.fetchall()
            conn.rollback()
        out = []
        for schema, name, kind, cols, rls in rows:
            rtype = "view" if kind in ("v", "m") else "table"
            out.append(
                Resource(
                    resource_type=rtype,
                    resource_id=f"{schema}.{name}",
                    name=name,
                    parent_id=schema,
                    schema={"columns": cols or []},
                    extra={"row_level_security": bool(rls), "relkind": kind},
                )
            )
        return out

    async def query(self, request: QueryRequest) -> QueryResult:
        return await asyncio.to_thread(self._query_sync, request)

    def _query_sync(self, request: QueryRequest) -> QueryResult:
        max_rows = self.config.max_rows if request.max_rows is None else request.max_rows
        if max_rows > self.config.max_rows:
            raise PolicyError("max_rows exceeds the connection cap", details={"cap": self.config.max_rows})
        timeout_ms = min(int(request.timeout_seconds * 1000), self.config.statement_timeout_ms)
        safe_sql = guard_read_only(request.sql)
        bounded = wrap_with_limit(safe_sql, max_rows)
        started = time.perf_counter()
        with self._connect() as conn, conn.cursor() as cur:
            self._begin_read_only(cur, timeout_ms, request.user_context)
            try:
                cur.execute(bounded, dict(request.params) if request.params else None)
                rows = cur.fetchall()
                columns = [d.name for d in cur.description or []]
            except psycopg.errors.QueryCanceled as exc:
                raise RetryableError("query exceeded statement timeout", details={"timeout_ms": timeout_ms}) from exc
            except psycopg.errors.ReadOnlySqlTransaction as exc:
                raise PolicyError("write attempted inside a read-only transaction") from exc
            except psycopg.errors.InsufficientPrivilege as exc:
                raise ScopeError("role lacks privilege for this query") from exc
            except psycopg.errors.UndefinedTable as exc:
                raise ValidationError("relation not found or not visible", details={"error": _safe(exc)}) from exc
            except (
                psycopg.errors.SyntaxError,
                psycopg.errors.UndefinedColumn,
                psycopg.errors.UndefinedFunction,
                psycopg.errors.DatatypeMismatch,
                psycopg.errors.InvalidTextRepresentation,
                psycopg.ProgrammingError,
            ) as exc:
                raise ValidationError("query rejected by database", details={"error": _safe(exc)}) from exc
            finally:
                conn.rollback()
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return QueryResult(
            columns=columns,
            rows=[list(r) for r in rows],
            truncated=truncated,
            row_count=len(rows),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


def _safe(exc: BaseException) -> str:
    return str(exc).split("\n")[0][:300]


ADAPTER = PostgresBoundedQuery
