"""Amazon S3: scoped list/fetch of objects under allowlisted prefixes, versions and delete markers.

Built on boto3/botocore (the official SDK; its service model is the operation contract). Verified so far only
against botocore's stubbed responses: an emulator is not AWS verification, and no AWS account was available
(docs/credential-matrix.md). Object ACLs are not read (acl_coverage=container): bucket/prefix grants decide access.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, NoCredentialsError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from wayam_connectors.contracts.capabilities import CheckResult, DocumentContent, Resource, SyncPage, SyncRequest
from wayam_connectors.contracts.envelope import Operation, SourceEnvelope, canonical_checksum
from wayam_connectors.contracts.errors import (
    AuthError,
    ConnectorError,
    PolicyError,
    RateLimitedError,
    RetryableError,
    ScopeError,
    UnsupportedOperationError,
    ValidationError,
)
from wayam_connectors.contracts.identity import AdapterContext
from wayam_connectors.mappings.knowledge.s3 import MAPPING_VERSION, map_object
from wayam_connectors.platform.security.ssrf import _reject_private_host

ADAPTER_VERSION = "0.1.0"
CONNECTOR_ID = "aws.s3"
CURSOR_VERSION = "1"
_AUTH_CODES = {"InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "InvalidToken", "AuthFailure", "TokenRefreshRequired"}
_SCOPE_CODES = {"AccessDenied", "AllAccessDisabled", "NoSuchBucket", "NoSuchKey", "NoSuchVersion", "403", "404", "NotFound"}
_RATE_CODES = {"SlowDown", "Throttling", "ThrottlingException", "RequestLimitExceeded", "503"}


class S3Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bucket: str = Field(min_length=3, max_length=63)
    region: str = Field(default="us-east-1", pattern=r"^[a-z]{2}-[a-z]+-\d$")
    allowed_prefixes: list[str] = Field(min_length=1, description="Only keys under these prefixes are listed or fetched")
    endpoint_url: str | None = Field(default=None, description="Only for customer gateways/emulators; https required")
    auth_mode: str = Field(default="access_key", pattern=r"^(access_key|ambient|assume_role)$")
    role_arn: str | None = None
    external_id: str | None = None
    use_versions: bool = Field(default=False, description="List object versions and delete markers (bucket versioning required)")
    max_object_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    page_size: int = Field(default=1000, ge=1, le=1000)
    allow_private_network: bool = False

    @field_validator("allowed_prefixes")
    @classmethod
    def _prefixes(cls, v: list[str]) -> list[str]:
        for p in v:
            if p.startswith("/") or ".." in p.split("/"):
                raise ValueError(f"invalid prefix {p!r}")
        return v

    @field_validator("endpoint_url")
    @classmethod
    def _https(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("https://"):
            raise ValueError("endpoint_url must be https")
        return v


class S3Secret(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None


def classify_client_error(exc: ClientError) -> ConnectorError:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    details = {"code": code, "status": status}
    if code in _AUTH_CODES:
        return AuthError("S3 rejected the credentials", details=details)
    if code in _RATE_CODES or status == 503:
        return RateLimitedError("S3 throttling", details=details)
    if code in _SCOPE_CODES or status in (403, 404):
        return ScopeError("S3 denied access or the object/bucket does not exist", details=details)
    if status and status >= 500:
        return RetryableError("S3 server error", details=details)
    return ValidationError("S3 rejected the request", details=details)


class S3Objects:
    CONNECTOR_ID = CONNECTOR_ID
    ADAPTER_VERSION = ADAPTER_VERSION

    def __init__(
        self, config: Mapping[str, Any], secret: Mapping[str, Any], context: AdapterContext | None = None, *, client: Any = None
    ) -> None:
        try:
            self.config = S3Config.model_validate(dict(config))
            self.secret = S3Secret.model_validate(dict(secret))
        except PydanticValidationError as exc:
            raise ValidationError(
                "invalid S3 connection config/secret",
                details={"errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors(include_input=False, include_url=False)]},
            ) from exc
        if self.config.auth_mode == "access_key" and not (self.secret.access_key_id and self.secret.secret_access_key):
            raise ValidationError("auth_mode=access_key needs access_key_id and secret_access_key")
        if self.config.auth_mode == "assume_role" and not self.config.role_arn:
            raise ValidationError("auth_mode=assume_role needs role_arn")
        if self.config.endpoint_url and not self.config.allow_private_network:
            from urllib.parse import urlsplit

            _reject_private_host(urlsplit(self.config.endpoint_url).hostname or "", resolve=False)
        self.context = context
        self._client = client  # injected for tests (botocore Stubber); built lazily otherwise

    # ------------------------------------------------------------ client

    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self) -> Any:
        c = self.config
        boto_cfg = BotoConfig(
            region_name=c.region, retries={"max_attempts": 0}, connect_timeout=5, read_timeout=30, user_agent_extra="wayam-connectors/0.1"
        )
        kwargs: dict[str, Any] = {"config": boto_cfg, "endpoint_url": c.endpoint_url}
        if c.auth_mode == "access_key":
            kwargs.update(
                aws_access_key_id=self.secret.access_key_id,
                aws_secret_access_key=self.secret.secret_access_key,
                aws_session_token=self.secret.session_token,
            )
        elif c.auth_mode == "assume_role":
            sts = boto3.client("sts", config=boto_cfg)
            try:
                assume: dict[str, Any] = {"RoleArn": c.role_arn, "RoleSessionName": "wayam-connectors"}
                if c.external_id:
                    assume["ExternalId"] = c.external_id
                creds = sts.assume_role(**assume)["Credentials"]
            except ClientError as exc:
                raise classify_client_error(exc) from exc
            kwargs.update(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
        return boto3.client("s3", **kwargs)

    def _call(self, op: str, **params: Any) -> Any:
        try:
            return getattr(self.client(), op)(**params)
        except ClientError as exc:
            raise classify_client_error(exc) from exc
        except NoCredentialsError as exc:
            raise AuthError("no AWS credentials available for ambient auth") from exc
        except EndpointConnectionError as exc:
            raise RetryableError("cannot reach the S3 endpoint") from exc
        except BotoCoreError as exc:
            raise RetryableError("S3 SDK failure", details={"error": type(exc).__name__}) from exc

    def _require_key(self, key: str) -> str:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise ValidationError("invalid object key")
        if not any(key.startswith(p) for p in self.config.allowed_prefixes):
            raise PolicyError("object key is outside the allowed prefixes", details={"key": key})
        return key

    # ------------------------------------------------------------ capabilities

    async def check_connection(self) -> CheckResult:
        return await asyncio.to_thread(self._check_sync)

    def _check_sync(self) -> CheckResult:
        c = self.config
        self._call("head_bucket", Bucket=c.bucket)
        for prefix in c.allowed_prefixes:
            self._call("list_objects_v2", Bucket=c.bucket, Prefix=prefix, MaxKeys=1)
        versioning = self._call("get_bucket_versioning", Bucket=c.bucket).get("Status")
        detail = f"bucket reachable; list permitted on {len(c.allowed_prefixes)} prefixes; versioning={versioning or 'off'}"
        if c.use_versions and versioning != "Enabled":
            return CheckResult(
                ok=False,
                identity=c.auth_mode,
                edition=f"S3 {c.region}",
                detail=detail + "; use_versions needs versioning Enabled",
                checked_at=datetime.now(UTC),
            )
        return CheckResult(ok=True, identity=c.auth_mode, edition=f"S3 {c.region}", detail=detail, checked_at=datetime.now(UTC))

    async def discover(self, resource_type: str | None = None) -> AsyncIterator[Resource]:
        if resource_type not in (None, "prefix"):
            raise UnsupportedOperationError("discover supports resource_type=prefix only")
        for p in self.config.allowed_prefixes:
            yield Resource(resource_type="prefix", resource_id=p, name=p, parent_id=self.config.bucket)

    async def sync_page(self, request: SyncRequest) -> SyncPage:
        return await asyncio.to_thread(self._sync_sync, request)

    def _sync_sync(self, request: SyncRequest) -> SyncPage:
        if request.resource_type != "object":
            raise UnsupportedOperationError("resource_type must be object")
        prefix = request.resource_id or ""
        if prefix not in self.config.allowed_prefixes:
            raise PolicyError("prefix is not allowlisted", details={"prefix": prefix})
        cursor = request.cursor or {}
        if cursor and cursor.get("v") != 1:
            raise ValidationError("unsupported cursor version")
        observed = datetime.now(UTC)
        iso = observed.isoformat()
        page_size = min(request.page_size, self.config.page_size)
        records: list[SourceEnvelope] = []
        if self.config.use_versions:
            params: dict[str, Any] = {"Bucket": self.config.bucket, "Prefix": prefix, "MaxKeys": page_size}
            if cursor.get("key_marker"):
                params["KeyMarker"] = cursor["key_marker"]
                if cursor.get("version_id_marker"):
                    params["VersionIdMarker"] = cursor["version_id_marker"]
            resp = self._call("list_object_versions", **params)
            for v in resp.get("Versions", []):
                doc = map_object(self.config.bucket, v, iso, version_id=v.get("VersionId"))
                records.append(self._envelope(doc, v, Operation.UPSERT, observed, v.get("LastModified")))
            for m in resp.get("DeleteMarkers", []):
                doc = map_object(self.config.bucket, m, iso, deleted=True, version_id=m.get("VersionId"))
                records.append(self._envelope(doc, m, Operation.DELETE, observed, m.get("LastModified")))
            truncated = bool(resp.get("IsTruncated"))
            if truncated and not resp.get("NextKeyMarker"):
                raise ValidationError("S3 reported a truncated version listing without a NextKeyMarker")
            next_cursor = (
                {"v": 1, "key_marker": resp.get("NextKeyMarker"), "version_id_marker": resp.get("NextVersionIdMarker")}
                if truncated
                else {"v": 1}
            )
        else:
            params = {"Bucket": self.config.bucket, "Prefix": prefix, "MaxKeys": page_size}
            if cursor.get("continuation_token"):
                params["ContinuationToken"] = cursor["continuation_token"]
            resp = self._call("list_objects_v2", **params)
            for o in resp.get("Contents", []):
                doc = map_object(self.config.bucket, o, iso)
                records.append(self._envelope(doc, o, Operation.UPSERT, observed, o.get("LastModified")))
            truncated = bool(resp.get("IsTruncated"))
            token = resp.get("NextContinuationToken")
            if truncated and not token:
                raise ValidationError("S3 reported a truncated listing without a NextContinuationToken")
            if truncated and token == cursor.get("continuation_token"):
                raise ValidationError("S3 returned the same continuation token twice (pagination cycle)")
            next_cursor = {"v": 1, "continuation_token": token} if truncated else {"v": 1}
        counts = {"object": len(records)}
        if isinstance(resp.get("KeyCount"), int):
            counts["key_count"] = int(resp["KeyCount"])
        return SyncPage(records=records, next_cursor=next_cursor, complete=not truncated, source_counts=counts)

    async def fetch_document(self, resource_id: str, version_id: str | None = None) -> DocumentContent:
        return await asyncio.to_thread(self._fetch_sync, resource_id, version_id)

    def _fetch_sync(self, key: str, version_id: str | None) -> DocumentContent:
        key = self._require_key(key)
        params: dict[str, Any] = {"Bucket": self.config.bucket, "Key": key}
        if version_id:
            params["VersionId"] = version_id
        head = self._call("head_object", **params)
        size = int(head.get("ContentLength", 0))
        if size > self.config.max_object_bytes:
            raise PolicyError("object exceeds the size limit", details={"size": size, "max": self.config.max_object_bytes})
        obj = self._call("get_object", **params)
        body = obj["Body"].read(self.config.max_object_bytes + 1)
        if len(body) > self.config.max_object_bytes:
            raise PolicyError("object exceeds the size limit while streaming")
        return DocumentContent(
            content=body,
            content_type=obj.get("ContentType"),
            size=len(body),
            etag=(obj.get("ETag") or "").strip('"') or None,
            version_id=obj.get("VersionId"),
        )

    def _envelope(self, doc: Any, raw: dict[str, Any], op: Operation, observed: datetime, last_modified: Any) -> SourceEnvelope:
        if self.context is None:
            raise PolicyError("adapter is not bound to a tenant/connection context")
        payload = doc.model_dump(mode="json")
        raw_clean = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in raw.items() if k != "Owner"}
        return SourceEnvelope(
            tenant_id=self.context.tenant_id,
            product=self.context.product.value,
            connection_id=self.context.connection_id,
            source_system=CONNECTOR_ID,
            source_edition=f"S3 {self.config.region}",
            source_object_id=doc.source_object_id,
            source_object_type="knowledge.document",
            schema_version="knowledge/1",
            mapping_version=MAPPING_VERSION,
            source_version=doc.source_version,
            operation=op,
            source_timestamp=last_modified if isinstance(last_modified, datetime) else None,
            observed_at=observed,
            checksum=canonical_checksum(payload),
            provenance=doc.path or f"s3://{self.config.bucket}/{doc.source_object_id}",
            payload=payload,
            extensions={"s3": raw_clean},
        )


ADAPTER = S3Objects
