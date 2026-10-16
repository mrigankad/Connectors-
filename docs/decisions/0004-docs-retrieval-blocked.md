# ADR-0004: Provider docs could not be retrieved in the build session; contracts are labelled accordingly

Date: 2026-10-16. Status: accepted.

Outbound HTTPS from the build container is policy-filtered: `learn.microsoft.com`, `docs.github.com` and other
vendor documentation hosts return `403` from the egress proxy, while PyPI is reachable. The prompt requires
recording this instead of inventing endpoints.

Consequence: `docs/provider-contracts/*.md` for S3, SharePoint, OneDrive, GitHub, GitHub Actions, Jira Cloud,
Vercel and SQL Server are marked **docs not retrieved in session** and list what must be verified (endpoint
versions, scopes, pagination, delta/webhook semantics, rate-limit headers, error bodies) before any adapter for
them moves past `contract_ready`. The implemented adapters (CSV, PostgreSQL, DOCX, PPTX) depend on stdlib,
libpq/psycopg and python-docx/python-pptx whose behaviour was verified locally, not on remote vendor APIs.
