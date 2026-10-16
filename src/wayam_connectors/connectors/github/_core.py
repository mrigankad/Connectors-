"""Shared GitHub provider core: configuration, authentication header, error semantics, pagination.

Contract basis (docs/provider-contracts/github-repositories.md): REST API with
`X-GitHub-Api-Version: 2022-11-28` (the version the live API selected on 2026-10-16),
`Accept: application/vnd.github+json`, `Link` header pagination, `X-RateLimit-*` headers,
401 for bad credentials, 403 + rate-limit headers for primary/secondary limits, 404 for
repositories the credential cannot see. Official docs were not retrievable from the build
session; behaviour recorded here was observed live against api.github.com.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from wayam_connectors.contracts.errors import (
    AuthError,
    ConnectorError,
    DataError,
    IntegrityError,
    PolicyError,
    RateLimitedError,
    ScopeError,
    ValidationError,
)
from wayam_connectors.contracts.identity import AdapterContext
from wayam_connectors.platform.http import ProviderHttp, classify_response, parse_link_header, retry_after_seconds
from wayam_connectors.platform.security.ssrf import OriginPolicy

API_VERSION = "2022-11-28"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_PAGES_PER_CALL = 50


class GitHubConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    api_base: str = "https://api.github.com"
    repositories: list[str] = Field(min_length=1, description="Allowlist of owner/name; nothing outside it is ever requested")
    auth_mode: str = Field(
        default="token",
        pattern=r"^(token|ambient)$",
        description="token: bearer token from the secret; ambient: credentials injected by an egress gateway",
    )
    page_size: int = Field(default=100, ge=1, le=100)
    require_read_only_credential: bool = Field(default=True, description="Fail the connection check if the credential can push")
    log_download_origins: list[str] = Field(
        default_factory=lambda: ["https://results-receiver.actions.githubusercontent.com"],
        description="Storage origins the Actions log redirect may go to; observed live on 2026-10-16, may change",
    )

    @field_validator("repositories")
    @classmethod
    def _repos(cls, v: list[str]) -> list[str]:
        for r in v:
            if not _REPO_RE.match(r):
                raise ValueError(f"repository must be owner/name, got {r!r}")
        return v

    @field_validator("api_base")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("api_base must be https")
        return v.rstrip("/")


class GitHubSecret(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    token: str | None = None
    webhook_secret: str | None = Field(default=None, description="Shared secret configured on the GitHub webhook")


def github_classifier(resp: httpx.Response) -> ConnectorError | None:
    s = resp.status_code
    if s == 410:
        return DataError("resource expired (retention)", details={"status": 410})
    if s == 403:
        body = resp.text[:300]
        if (
            "rate limit" in body.lower()
            or resp.headers.get("x-ratelimit-remaining") == "0"
            or retry_after_seconds(resp.headers) is not None
        ):
            return RateLimitedError("GitHub rate limit", retry_after_seconds=retry_after_seconds(resp.headers), details={"status": 403})
        return ScopeError("GitHub forbade the request (missing permission or SSO)", details={"status": 403})
    if s == 404:
        return ScopeError("repository/resource not found or credential lacks access", details={"status": 404})
    if s == 401:
        return AuthError("GitHub rejected the credential", details={"status": 401})
    return classify_response(resp)


def validate_config(config: Mapping[str, Any]) -> GitHubConfig:
    try:
        return GitHubConfig.model_validate(dict(config))
    except PydanticValidationError as exc:
        raise ValidationError("invalid GitHub connection config", details={"errors": _errs(exc)}) from exc


def validate_secret(secret: Mapping[str, Any]) -> GitHubSecret:
    try:
        return GitHubSecret.model_validate(dict(secret))
    except PydanticValidationError as exc:
        raise ValidationError("invalid GitHub secret shape", details={"errors": _errs(exc)}) from exc


def _errs(exc: PydanticValidationError) -> list[dict[str, Any]]:
    return [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors(include_input=False, include_url=False)]


class GitHubClient:
    def __init__(
        self,
        config: GitHubConfig,
        secret: GitHubSecret,
        context: AdapterContext | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve_dns: bool | None = None,
    ) -> None:
        self.config = config
        self.context = context
        if config.auth_mode == "token" and not secret.token:
            raise ValidationError("auth_mode=token needs a token in the secret")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        if config.auth_mode == "token" and secret.token:
            headers["Authorization"] = f"Bearer {secret.token}"
        self.http = ProviderHttp(
            OriginPolicy.for_origins([config.api_base]),
            base_headers=headers,
            classifier=github_classifier,
            transport=transport,
            resolve_dns=resolve_dns,
        )
        self.log_policy = OriginPolicy.for_origins(config.log_download_origins)

    def require_repo(self, full_name: str | None) -> tuple[str, str]:
        if not full_name or full_name not in self.config.repositories:
            raise PolicyError("repository is not in this connection's allowlist", details={"repository": full_name})
        owner, name = full_name.split("/", 1)
        return owner, name

    def url(self, path: str) -> str:
        return f"{self.config.api_base}{path}"

    async def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> tuple[Any, httpx.Response]:
        return await self.http.get_json(self.url(path), params=params)

    async def paginate(
        self, path: str, params: Mapping[str, Any] | None = None, *, start_url: str | None = None, max_pages: int = MAX_PAGES_PER_CALL
    ) -> AsyncIterator[tuple[list[Any], str | None]]:
        """Yield (items, next_url) following Link rel=next. Detects repeated page URLs (cycle) and caps pages per call."""
        url = start_url or self.url(path)
        seen: set[str] = set()
        for _ in range(max_pages):
            if url in seen:
                raise IntegrityError("pagination cycle detected", details={"url": url.split("?")[0]})
            seen.add(url)
            data, resp = await self.http.get_json(url, params=params if url == self.url(path) else None)
            nxt = parse_link_header(resp.headers.get("link")).get("next")
            yield data, nxt
            if not nxt:
                return
            url = nxt
        # page budget spent: the last yielded next_url is the caller's resume cursor

    async def aclose(self) -> None:
        await self.http.aclose()
