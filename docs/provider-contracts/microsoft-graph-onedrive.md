# Provider contract: OneDrive for Business (WCN-050)

Status: **contract_ready pending verification — vendor docs not retrieved in the build session** (egress policy
returned 403 for the documentation host; see ADR-0004). Catalog docs link: https://learn.microsoft.com/en-us/graph/api/resources/driveitem

Nothing below is a verified endpoint claim. Before implementing, retrieve the current official docs and record here:
exact endpoint/API version, edition/region, supported identity modes, least required scopes, pagination, filters,
incremental/delete/ACL behaviour, rate limits, error-body semantics and the sandbox access needed.

Specific items to verify for this surface: Same transport as SharePoint; user drives vs app-only access, delta per drive, identity-mode differences (delegated user vs app with Sites.Selected does not cover personal drives), deletions in delta, no assumption of SharePoint ACL parity.

Implementation rule: build against a clearly labelled fixture contract only after this page is filled in; the
adapter's manifest `verification_level` stays `fixture_tested` until a live test against a real tenant passes.
