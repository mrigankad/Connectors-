# Provider contract: GitHub Actions (`github.actions`, WCN-099)

Verified: 2026-10-16, live against `https://api.github.com` for `mrigankad/Chakram` (ambient credentials, see the
repositories contract). Docs not retrievable in session (ADR-0004): *observed* vs *knowledge* as below.

| Item | Contract | Basis |
|---|---|---|
| Endpoints | `GET /repos/{o}/{r}/actions/workflows`, `.../actions/runs?per_page&created=>=YYYY-MM-DD`, `.../actions/runs/{id}/jobs`, `.../actions/runs/{id}/artifacts`, `.../actions/runs/{id}/logs` (302 to storage) | workflows/runs/jobs/artifacts/logs-302 observed; `created` filter syntax: knowledge |
| Scopes | actions:read (+ metadata:read) | knowledge |
| Run identity | `id` immutable; version = `updated_at#run_attempt` so re-runs create new versions | observed fields |
| Jobs | `jobs[]` with `id`, `run_id`, `name`, `status`, `conclusion`, `started_at`, `completed_at`, `run_attempt` | observed |
| Artifacts | `artifacts[]` with `id`, `name`, `size_in_bytes`, `expired`, `expires_at` (fixture empty: repo had none) | field names: knowledge |
| Logs | 302 `Location` to `https://results-receiver.actions.githubusercontent.com/...` (pre-signed; never logged). Adapter follows one redirect only to allowlisted origins, strips the API token and media headers, inspects the zip, returns redacted text. 410 -> `DataError("expired")`; logs of an in-progress run return 404 (-> ScopeError) | redirect origin and in-progress 404 observed; zip layout and 410: knowledge |
| Mutations | dispatch/rerun/cancel deliberately absent; the shared transport refuses bare POST | by construction (T-44 test) |

Live evidence (2026-10-16): check_connection OK (1 workflow), workflow_run sync (7 runs total, paginated), jobs for the
latest run (3 jobs), artifacts (0). **Log download BLOCKED**: the storage origin is denied by the build session's
egress policy (CONNECT 403), so `fetch_document` is fixture-tested only.
