# Provider contract: GitHub repositories (WCN-097)

Status: **contract_ready pending verification — vendor docs not retrieved in the build session** (egress policy
returned 403 for the documentation host; see ADR-0004). Catalog docs link: https://docs.github.com/en/rest

Nothing below is a verified endpoint claim. Before implementing, retrieve the current official docs and record here:
exact endpoint/API version, edition/region, supported identity modes, least required scopes, pagination, filters,
incremental/delete/ACL behaviour, rate limits, error-body semantics and the sandbox access needed.

Specific items to verify for this surface: REST API version header (X-GitHub-Api-Version), fine-grained PAT / GitHub App installation token scopes (contents:read, pull_requests:read, issues:read, metadata:read), Link-header pagination, rate-limit headers (primary/secondary), webhooks (X-Hub-Signature-256 over raw body, X-GitHub-Delivery), immutable repository id vs renamed owner/name.

Implementation rule: build against a clearly labelled fixture contract only after this page is filled in; the
adapter's manifest `verification_level` stays `fixture_tested` until a live test against a real tenant passes.
