# Provider contract: Jira Cloud (WCN-084)

Status: **contract_ready pending verification — vendor docs not retrieved in the build session** (egress policy
returned 403 for the documentation host; see ADR-0004). Catalog docs link: https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/

Nothing below is a verified endpoint claim. Before implementing, retrieve the current official docs and record here:
exact endpoint/API version, edition/region, supported identity modes, least required scopes, pagination, filters,
incremental/delete/ACL behaviour, rate limits, error-body semantics and the sandbox access needed.

Specific items to verify for this surface: REST v3 search endpoint (JQL, nextPageToken pagination replacing startAt), OAuth 2.0 (3LO) scopes read:jira-work vs API token basic auth, changelog expansion, project permission scheme and issue security levels as ACLs, ADF comment bodies, 429 handling; Jira Data Center is a separate surface.

Implementation rule: build against a clearly labelled fixture contract only after this page is filled in; the
adapter's manifest `verification_level` stays `fixture_tested` until a live test against a real tenant passes.
