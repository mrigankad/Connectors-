# Provider contract: GitHub Actions (WCN-099)

Status: **contract_ready pending verification — vendor docs not retrieved in the build session** (egress policy
returned 403 for the documentation host; see ADR-0004). Catalog docs link: https://docs.github.com/en/rest/actions

Nothing below is a verified endpoint claim. Before implementing, retrieve the current official docs and record here:
exact endpoint/API version, edition/region, supported identity modes, least required scopes, pagination, filters,
incremental/delete/ACL behaviour, rate limits, error-body semantics and the sandbox access needed.

Specific items to verify for this surface: actions:read permission, workflow runs/jobs/artifacts endpoints, log download redirect (short-lived signed URL, never logged), artifact retention/expiry, secret redaction already applied by GitHub in logs plus our own redaction, dispatch/rerun endpoints deliberately NOT granted.

Implementation rule: build against a clearly labelled fixture contract only after this page is filled in; the
adapter's manifest `verification_level` stays `fixture_tested` until a live test against a real tenant passes.
