from __future__ import annotations

import json

import httpx
import pytest

from wayam_connectors.contracts.errors import AuthError, PolicyError, RateLimitedError, RetryableError, ScopeError, ValidationError
from wayam_connectors.platform.http import ProviderHttp, parse_link_header, retry_after_seconds
from wayam_connectors.platform.security.ssrf import OriginPolicy

POLICY = OriginPolicy.for_origins(["https://api.example.com"])


def _client(handler, **kw):
    return ProviderHttp(POLICY, transport=httpx.MockTransport(handler), resolve_dns=False, **kw)


def test_link_and_retry_after_parsing():
    link = '<https://api.example.com/x?page=2>; rel="next", <https://api.example.com/x?page=9>; rel="last"'
    assert parse_link_header(link) == {"next": "https://api.example.com/x?page=2", "last": "https://api.example.com/x?page=9"}
    assert parse_link_header(None) == {}
    assert retry_after_seconds({"Retry-After": "7"}) == 7.0
    assert retry_after_seconds({"retry-after": "Thu, 01 Jan 2099 00:00:00 GMT"}) > 1000
    assert retry_after_seconds({"Retry-After": "garbage"}) is None


async def test_status_classification():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/text":
            return httpx.Response(200, text="not json")
        code = int(req.url.path.strip("/"))
        headers = {}
        if code == 403 and "rl" in str(req.url.query):
            headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}
        if code == 429:
            headers = {"Retry-After": "3"}
        return httpx.Response(code, headers=headers, text='{"message":"token ghp_abcdefghijklmnopqrstuvwxyz0123456789"}')

    async with _client(handler) as c:
        with pytest.raises(AuthError) as e:
            await c.request("GET", "https://api.example.com/401")
        assert "ghp_" not in json.dumps(e.value.details)
        with pytest.raises(ScopeError):
            await c.request("GET", "https://api.example.com/403")
        with pytest.raises(RateLimitedError):
            await c.request("GET", "https://api.example.com/403?rl=1")
        with pytest.raises(ScopeError):
            await c.request("GET", "https://api.example.com/404")
        with pytest.raises(RateLimitedError) as rl:
            await c.request("GET", "https://api.example.com/429")
        assert rl.value.retry_after_seconds == 3.0
        with pytest.raises(RetryableError):
            await c.request("GET", "https://api.example.com/503")
        with pytest.raises(ValidationError):
            await c.request("GET", "https://api.example.com/422")
        with pytest.raises(ValidationError):
            await c.get_json("https://api.example.com/text")  # body is not JSON


async def test_policy_redirects_and_limits():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "https://storage.example.net/blob?sig=abc123456789"})
        if req.url.path == "/loop":
            return httpx.Response(302, headers={"Location": "/loop"})
        if req.url.path == "/big":
            return httpx.Response(200, headers={"Content-Length": "999999999"}, content=b"x")
        if req.url.host == "storage.example.net":
            assert req.headers.get("authorization", "") == ""  # provider token never forwarded to storage
            return httpx.Response(200, content=b"log data")
        return httpx.Response(200, json={"ok": True})

    async with _client(handler, base_headers={"Authorization": "Bearer t0ken"}) as c:
        with pytest.raises(PolicyError):
            await c.request("GET", "https://evil.example.org/")  # not allowlisted
        with pytest.raises(PolicyError):
            await c.request("GET", "http://api.example.com/")  # plain http
        with pytest.raises(PolicyError):
            await c.request("POST", "https://api.example.com/x")  # bare mutation through shared transport
        with pytest.raises(PolicyError):
            await c.request("GET", "https://api.example.com/redirect")  # redirect to a non-allowlisted origin
        resp = await c.request(
            "GET", "https://api.example.com/redirect", allow_redirect_to=OriginPolicy.for_origins(["https://storage.example.net"])
        )
        assert resp.content == b"log data"
        with pytest.raises(PolicyError):
            await c.request("GET", "https://api.example.com/loop")
        with pytest.raises(PolicyError):
            await c.request("GET", "https://api.example.com/big")
        data, _ = await c.get_json("https://api.example.com/ok")
        assert data == {"ok": True}


async def test_transport_errors_are_retryable():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    async with _client(handler) as c:
        with pytest.raises(RetryableError):
            await c.request("GET", "https://api.example.com/x")
