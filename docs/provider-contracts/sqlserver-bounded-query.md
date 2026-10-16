# Provider contract: Microsoft SQL Server (WCN-141)

Status: **contract_ready pending verification — vendor docs not retrieved in the build session** (egress policy
returned 403 for the documentation host; see ADR-0004). Catalog docs link: https://learn.microsoft.com/en-us/sql/connect/

Nothing below is a verified endpoint claim. Before implementing, retrieve the current official docs and record here:
exact endpoint/API version, edition/region, supported identity modes, least required scopes, pagination, filters,
incremental/delete/ACL behaviour, rate limits, error-body semantics and the sandbox access needed.

Specific items to verify for this surface: Driver choice (pyodbc + ODBC Driver 18 vs pymssql), TLS/encryption defaults, Entra ID / SQL auth modes, READ ONLY semantics (SET TRANSACTION ISOLATION, no READ ONLY transaction mode), statement timeout mechanism (query timeout on the driver), TOP vs OFFSET/FETCH row capping, RLS via SESSION_CONTEXT, error number semantics.

Implementation rule: build against a clearly labelled fixture contract only after this page is filled in; the
adapter's manifest `verification_level` stays `fixture_tested` until a live test against a real tenant passes.
