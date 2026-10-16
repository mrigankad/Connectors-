# Provider contract: GitHub repositories (`github.repositories`, WCN-097)

Verified: 2026-10-16, live against `https://api.github.com` for `mrigankad/Chakram` using credentials injected by the
build session's egress gateway (`auth_mode=ambient`). Official documentation (docs.github.com) was **not retrievable**
from the session (ADR-0004): every item below is marked *observed* (seen in live responses, captured under
`fixtures/github/`) or *knowledge* (from engineering knowledge of the REST API, to be confirmed against current docs).

| Item | Contract | Basis |
|---|---|---|
| API version | `X-GitHub-Api-Version: 2022-11-28`; response `X-Github-Api-Version-Selected: 2022-11-28` | observed |
| Media type | `Accept: application/vnd.github+json` | observed |
| Identity modes | `token` (fine-grained PAT / installation token as `Authorization: Bearer`), `ambient` (gateway-injected). GitHub App token minting not implemented | knowledge / observed (ambient) |
| Least scopes | metadata:read, contents:read (commits), pull_requests:read, issues:read | knowledge |
| Endpoints | `GET /repos/{owner}/{repo}`, `GET /repos/{owner}/{repo}/commits?sha&since&per_page`, `GET /repos/{owner}/{repo}/pulls?state=all&sort=updated&direction=asc`, `GET /repos/{owner}/{repo}/issues?state=all&since&sort=updated&direction=asc` | observed (repo, commits, pulls, issues return 200 live); `since` on commits/issues: knowledge |
| Pagination | `Link: <...>; rel="next"` with `/repositories/{id}/...` URLs; adapter resumes from the Link URL; repeated URL = cycle error | observed |
| Rate limits | `X-RateLimit-Limit/Remaining/Reset/Resource`; 403 with remaining 0 or `Retry-After` -> `RateLimitedError`; 429 likewise | observed headers (15000/h with the injected credential); 403 body semantics: knowledge |
| Errors | 401 -> AuthError (blocks connection); 403 -> ScopeError; 404 -> ScopeError (no existence disclosure for private repos) | 404 observed for an unknown repo; others: knowledge |
| Incremental | commits: `since` watermark (committer date) with overlap; PRs: full pass sorted by `updated` asc, client-side watermark; issues: `since` + watermark; PRs excluded from issues via `pull_request` key | knowledge, exercised on fixtures |
| Deletes | issue `deleted` webhook event only | knowledge |
| Webhooks | `X-Hub-Signature-256` = `sha256=` + HMAC-SHA256(secret, raw body); `X-GitHub-Delivery` dedup id; `X-GitHub-Event` | knowledge, exercised on fixtures |
| Permissions check | `permissions.push/admin/maintain` in the repo object reveal a write-capable credential; `require_read_only_credential` fails the check | observed (the injected credential is admin) |

Live evidence (`tests/live/test_github_live.py`, 2026-10-16): check_connection OK, discover, commit sync full pass + watermark
re-sync, PR and issue sync (both empty on this repository), allowlist rejection, 404 on an unknown repository.
Not live-verified: token-mode authentication, private repositories, webhooks (no inbound endpoint exposed), PR/issue
content (synthetic fixtures only).
