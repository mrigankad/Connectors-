# Credential matrix

What each surface needs to move beyond local/fixture evidence. Nothing here exists yet in this repository; no
credential is stored in source. Local tests use `WAYAM_LOCAL_SECRET_KEY` (dev only) and the local PostgreSQL role.

| Surface | Identity mode to test | Least scope | Sandbox needed | Owner to provide | Status |
|---|---|---|---|---|---|
| files.csv / docx / pptx | none (inherited) | – | Word/PowerPoint-authored sample files, sanitized | Wayam product teams | fixtures present (generated); authored samples missing |
| postgresql.bounded_query | read-only DB role, TLS | CONNECT, USAGE, SELECT on allowlisted schemas | customer-like DB with RLS | Bodham/SAMYAK pilot customer | local verified; customer DB pending |
| SQL Server | read-only login or Entra ID | SELECT on approved views | SQL Server 2019+/Azure SQL instance | Bodham pilot | blocked: no instance, driver decision pending |
| Amazon S3 | IAM role (STS assume-role) | s3:ListBucket, s3:GetObject, s3:GetObjectVersion on allowlisted prefixes | real AWS test bucket (emulator does not count) | Wayam infra | blocked: no AWS account in session |
| SharePoint Online | Entra app, Sites.Selected (app) + delegated user for authorization checks | Sites.Selected, Files.Read.Selected | M365 developer tenant with test users/groups | Wayam IT | blocked: no tenant |
| OneDrive for Business | delegated and app-only | Files.Read / Files.Read.All (edition-dependent) | same tenant | Wayam IT | blocked |
| GitHub repositories | fine-grained PAT, then GitHub App installation | contents:read, pull_requests:read, issues:read, metadata:read | test org + repo | Chakram team | blocked: no token in session |
| GitHub Actions | same | actions:read | same | Chakram team | blocked |
| Jira Cloud | API token (basic) then OAuth 2.0 3LO | read:jira-work | Atlassian dev site | Chakram team | blocked |
| Vercel | team-scoped token | read-only project/deployment access | Vercel team with a test project | Chakram team | blocked |
| Azure Key Vault (secret backend) | managed identity | wrapKey/unwrapKey on one key | Key Vault + identity | Wayam infra | blocked: not implemented (ADR-0003) |
