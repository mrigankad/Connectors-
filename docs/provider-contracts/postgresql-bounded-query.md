# Provider contract: PostgreSQL bounded read queries (`postgresql.bounded_query`, WCN-140)

Verified: 2026-10-16 against PostgreSQL 16.15 (Ubuntu) with psycopg 3.3.6. Vendor docs
(https://www.postgresql.org/docs/current/) not retrieved in session; behaviour verified by integration tests.

| Item | Contract |
|---|---|
| Edition | PostgreSQL 13+ (server_version_num checked). Password auth; `sslmode` require/verify-ca/verify-full/disable. |
| Identity mode | Dedicated read-only role. `check_connection` fails if the role is superuser/createrole/createdb or holds INSERT/UPDATE/DELETE/TRUNCATE on any object in the allowlisted schemas. |
| Discovery | Tables/views in `allowed_schemas` the role can SELECT, with columns and `relrowsecurity`. |
| Query | One statement; static guard (`sql_guard.py`) rejects writes, multi-statements, `FOR UPDATE/SHARE` (DB-side), `pg_sleep`, file/admin functions, `SET`, `COPY`, sequences. Then `SET TRANSACTION READ ONLY`, `SET LOCAL statement_timeout`, `search_path = pg_catalog`, server-side parameters, `LIMIT max_rows+1` to detect truncation. |
| Per-user data policy | `user_context` → `set_config(<user_context_setting>, value, true)`; the customer's RLS policies read it. Without the setting configured, `user_context` is `UnsupportedOperationError`, never silently ignored. |
| Errors | Credentials → `AuthError`; privilege → `ScopeError`; timeout → `RetryableError`; syntax/unknown relation → `ValidationError`; write inside READ ONLY → `PolicyError`. |
| Not supported | CDC/record sync, IAM/Entra token auth, cross-database queries, cursors/streaming. |

Tests: `tests/unit/test_sql_guard.py`, `tests/integration/test_postgresql_connector.py`.
