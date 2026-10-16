# Provider contract: Vercel (WCN-109)

Status: **contract_ready pending verification — vendor docs not retrieved in the build session** (egress policy
returned 403 for the documentation host; see ADR-0004). Catalog docs link: https://vercel.com/docs/rest-api

Nothing below is a verified endpoint claim. Before implementing, retrieve the current official docs and record here:
exact endpoint/API version, edition/region, supported identity modes, least required scopes, pagination, filters,
incremental/delete/ACL behaviour, rate limits, error-body semantics and the sandbox access needed.

Specific items to verify for this surface: Bearer token (team-scoped, teamId query param), projects and deployments list pagination (until/next), deployment states (BUILDING/READY/ERROR/CANCELED), deployment events, rate limits; deployment creation endpoint deliberately not granted.

Implementation rule: build against a clearly labelled fixture contract only after this page is filled in; the
adapter's manifest `verification_level` stays `fixture_tested` until a live test against a real tenant passes.
