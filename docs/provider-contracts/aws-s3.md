# Provider contract: Amazon S3 (WCN-119)

Status: **contract_ready pending verification — vendor docs not retrieved in the build session** (egress policy
returned 403 for the documentation host; see ADR-0004). Catalog docs link: https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html

Nothing below is a verified endpoint claim. Before implementing, retrieve the current official docs and record here:
exact endpoint/API version, edition/region, supported identity modes, least required scopes, pagination, filters,
incremental/delete/ACL behaviour, rate limits, error-body semantics and the sandbox access needed.

Specific items to verify for this surface: ListObjectsV2 pagination (ContinuationToken), ListObjectVersions and delete markers, GetObject conditional headers (ETag/If-Match), prefix allowlists, IAM role / STS assume-role identity, SSE-KMS grants, 503 SlowDown and Retry-After behaviour, request-count limits, event notifications (EventBridge/SQS) vs reconciliation; live test needs a real bucket (an emulator is not AWS verification).

Implementation rule: build against a clearly labelled fixture contract only after this page is filled in; the
adapter's manifest `verification_level` stays `fixture_tested` until a live test against a real tenant passes.
