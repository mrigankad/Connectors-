# Provider contract: Amazon S3 (`aws.s3`, WCN-119)

Status: **fixture_tested** (2026-10-16). Built on boto3/botocore 1.43.114: the botocore service model is the
operation contract (request/response shapes are validated by the SDK itself). Official HTML docs were not
retrievable from the build session (ADR-0004). No AWS account or test bucket was available, so every behaviour
below was exercised only against `botocore.stub.Stubber` responses; an S3 emulator would not count as AWS
verification either.

| Item | Contract | Basis |
|---|---|---|
| Identity modes | `access_key` (+ optional session token), `assume_role` (STS AssumeRole with optional ExternalId), `ambient` (instance/task role or environment chain) | SDK model; assume_role/ambient untested |
| Least scopes | `s3:ListBucket` (+ `s3:ListBucketVersions` when `use_versions`), `s3:GetBucketVersioning`, `s3:GetObject`, `s3:GetObjectVersion`; `kms:Decrypt` for SSE-KMS objects | knowledge |
| Scope | one bucket, allowlisted key prefixes; keys outside the prefixes are refused for listing and fetching; `..` segments rejected | tests |
| Listing | `ListObjectsV2(Prefix, MaxKeys, ContinuationToken)`; cursor v1 = `{continuation_token}`; truncated without token or repeated token -> error | stubbed |
| Versions | `ListObjectVersions(Prefix, MaxKeys, KeyMarker, VersionIdMarker)`; `DeleteMarkers` become `delete` records; cursor v1 = `{key_marker, version_id_marker}` | stubbed |
| Fetch | `HeadObject` size check against `max_object_bytes`, then `GetObject` (optional `VersionId`), streamed with a hard cap | stubbed |
| Errors | InvalidAccessKeyId/SignatureDoesNotMatch/ExpiredToken -> AuthError; AccessDenied/NoSuchBucket/NoSuchKey/403/404 -> ScopeError; SlowDown/503/Throttling -> RateLimitedError; endpoint failures -> RetryableError | stubbed (subset) |
| Deletes | `deletion_semantics=full_crawl_reconcile`: inferred only after a complete crawl of a prefix; delete markers are explicit events when versioning is on | engine tests |
| ACLs | not read (`acl_coverage=container`); S3 object ACLs/bucket policies are not mapped to principals | design |

Required before `sandbox_verified`: a real bucket with versioning, prefixes with >1000 keys, SSE-KMS objects, an
IAM role for `assume_role`, and throttling behaviour observed live (`docs/credential-matrix.md`).
