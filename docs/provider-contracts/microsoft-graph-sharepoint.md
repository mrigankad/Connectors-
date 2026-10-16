# Provider contract: SharePoint Online files (WCN-049)

Status: **contract_ready pending verification — vendor docs not retrieved in the build session** (egress policy
returned 403 for the documentation host; see ADR-0004). Catalog docs link: https://learn.microsoft.com/en-us/graph/api/driveitem-delta

Nothing below is a verified endpoint claim. Before implementing, retrieve the current official docs and record here:
exact endpoint/API version, edition/region, supported identity modes, least required scopes, pagination, filters,
incremental/delete/ACL behaviour, rate limits, error-body semantics and the sandbox access needed.

Specific items to verify for this surface: Graph v1.0 driveItem delta (token lifetime, resync 410 behaviour, deleted facet), Sites.Selected / Files.SelectedOperations app permissions vs delegated, permissions endpoint for ACLs (users, groups, sharing links, inheritance), group membership expansion, throttling (429 + Retry-After, RateLimit headers), download via @microsoft.graph.downloadUrl (pre-signed, must be redacted).

Implementation rule: build against a clearly labelled fixture contract only after this page is filled in; the
adapter's manifest `verification_level` stays `fixture_tested` until a live test against a real tenant passes.
