"""Shared outbound HTTP transport for provider adapters.

* Every request URL (and every redirect target) passes the connection's `OriginPolicy`.
* Timeouts and a response-size cap are always set.
* Provider errors are classified into typed errors by a per-provider hook; the generic
  classification covers 401/403/404/408/429/5xx and transport failures.
* Nothing sensitive is kept on exceptions: headers are redacted, bodies are truncated.
* `Link` header pagination and `Retry-After` parsing are shared helpers.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from opentelemetry import trace

from wayam_connectors.contracts.errors import (
    AuthError,
    ConnectorError,
    PolicyError,
    RateLimitedError,
    RetryableError,
    ScopeError,
    ValidationError,
)

from .security.redaction import redact_headers, redact_text
from .security.ssrf import OriginPolicy
from .telemetry import span

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')

Classifier = Callable[[httpx.Response], ConnectorError | None]


def parse_link_header(value: str | None) -> dict[str, str]:
    return {rel: url for url, rel in _LINK_RE.findall(value or "")}


def retry_after_seconds(headers: Mapping[str, str], *, now: datetime | None = None) -> float | None:
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def classify_response(resp: httpx.Response) -> ConnectorError | None:
    """Generic classification; providers wrap this with their own body semantics."""
    s = resp.status_code
    if s < 400:
        return None
    body = _snippet(resp)
    details = {"status": s, "body": body}
    if s == 401:
        return AuthError("unauthorized", details=details)
    if s == 403:
        if retry_after_seconds(resp.headers) is not None or resp.headers.get("x-ratelimit-remaining") == "0":
            return RateLimitedError("rate limited", retry_after_seconds=_rate_limit_wait(resp), details=details)
        return ScopeError("forbidden", details=details)
    if s == 404:
        return ScopeError("not found or no access", details=details)
    if s == 429:
        return RateLimitedError("too many requests", retry_after_seconds=_rate_limit_wait(resp), details=details)
    if s in (408, 409, 425) or s >= 500:
        return RetryableError(f"provider error {s}", details=details)
    return ValidationError(f"provider rejected request ({s})", details=details)


def _rate_limit_wait(resp: httpx.Response) -> float | None:
    ra = retry_after_seconds(resp.headers)
    if ra is not None:
        return ra
    reset = resp.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return max(0.0, float(reset) - datetime.now(UTC).timestamp())
    return None


def _snippet(resp: httpx.Response, limit: int = 300) -> str:
    try:
        return redact_text(resp.text[:limit])
    except Exception:  # noqa: BLE001
        return "<unreadable>"


class ProviderHttp:
    """A policy-bound HTTP client. Adapters call `request`/`get_json`; they never build httpx clients themselves."""

    def __init__(
        self,
        policy: OriginPolicy,
        *,
        base_headers: Mapping[str, str] | None = None,
        classifier: Classifier | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "wayam-connectors/0.1",
        resolve_dns: bool | None = None,
    ) -> None:
        self.policy = policy
        # Behind an egress proxy (HTTPS_PROXY) the proxy resolves names and enforces its own policy; a local
        # getaddrinfo is then meaningless (and often unavailable). Direct egress keeps the private-IP check.
        self.resolve_dns = resolve_dns if resolve_dns is not None else not _proxy_in_use()
        self.classifier = classifier or classify_response
        self.max_response_bytes = max_response_bytes
        headers = {"User-Agent": user_agent, **(base_headers or {})}
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False, transport=transport)
        self.last_rate_limit: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ProviderHttp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        allow_redirect_to: OriginPolicy | None = None,
    ) -> httpx.Response:
        with span("provider.http", method=method.upper(), host=httpx.URL(url).host, path=httpx.URL(url).path):
            return await self._request(method, url, params=params, headers=headers, json=json, allow_redirect_to=allow_redirect_to)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        json: Any,
        allow_redirect_to: OriginPolicy | None,
    ) -> httpx.Response:
        resolve_dns = self.resolve_dns
        if method.upper() not in {"GET", "HEAD"} and json is None:
            # mutations go through ReviewedAction adapters that call `request` with an explicit json body;
            # a bare POST/PUT/DELETE here is almost always a bug
            raise PolicyError("non-read request without a body is not allowed through the shared transport")
        self.policy.check_url(url, resolve=resolve_dns)
        try:
            resp = await self._client.request(method, url, params=params, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise RetryableError("provider timeout", details={"url": _safe_url(url)}) from exc
        except httpx.TransportError as exc:
            raise RetryableError("transport failure", details={"url": _safe_url(url), "error": type(exc).__name__}) from exc
        hops = 0
        while resp.is_redirect:
            hops += 1
            target = str(resp.headers.get("location", ""))
            if hops > self.policy.max_redirects or not target:
                raise PolicyError("too many redirects or empty redirect target")
            target = str(httpx.URL(url).join(target))
            (allow_redirect_to or self.policy).check_url(target, resolve=resolve_dns)
            # redirects to pre-signed storage URLs must not carry the provider Authorization header or API media headers
            try:
                req = self._client.build_request("GET", target, headers=None if allow_redirect_to else headers)
                if allow_redirect_to:
                    for h in ("authorization", "accept", "x-github-api-version"):
                        req.headers.pop(h, None)
                    req.headers["accept"] = "*/*"
                resp = await self._client.send(req)
            except httpx.TransportError as exc:
                raise RetryableError("transport failure on redirect") from exc
        length = resp.headers.get("content-length")
        if length and length.isdigit() and int(length) > self.max_response_bytes:
            raise PolicyError("response exceeds size limit", details={"content_length": int(length)})
        if len(resp.content) > self.max_response_bytes:
            raise PolicyError("response exceeds size limit", details={"bytes": len(resp.content)})
        self.last_rate_limit = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}
        trace.get_current_span().set_attribute("http.status_code", resp.status_code)
        err = self.classifier(resp)
        if err is not None:
            err.details.setdefault("url", _safe_url(url))
            err.details["headers"] = redact_headers(dict(resp.headers))
            raise err
        return resp

    async def get_json(
        self, url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None
    ) -> tuple[Any, httpx.Response]:
        resp = await self.request("GET", url, params=params, headers=headers)
        try:
            return resp.json(), resp
        except ValueError as exc:
            raise ValidationError("provider returned non-JSON", details={"url": _safe_url(url), "body": _snippet(resp)}) from exc


def _proxy_in_use() -> bool:
    import os

    return any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"))


def _safe_url(url: str) -> str:
    u = httpx.URL(url)
    return f"{u.scheme}://{u.host}{u.path}"
