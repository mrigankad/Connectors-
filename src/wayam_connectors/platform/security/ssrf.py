"""Outbound destination policy: allowlisted origins, no private/metadata networks, validated redirects."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from wayam_connectors.contracts.errors import PolicyError

_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",  # cloud metadata
        "172.16.0.0/12",
        "192.168.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "::ffff:0:0/96",
    )
]


@dataclass(frozen=True)
class OriginPolicy:
    """Which origins an adapter may call. `allow_private` is only for the customer gateway."""

    allowed_origins: frozenset[str]
    allow_private: bool = False
    allowed_schemes: frozenset[str] = field(default_factory=lambda: frozenset({"https"}))
    max_redirects: int = 3

    @classmethod
    def for_origins(cls, origins: Iterable[str], *, allow_private: bool = False) -> OriginPolicy:
        norm = frozenset(_origin(o) for o in origins)
        schemes = frozenset({"https", "http"}) if allow_private else frozenset({"https"})
        return cls(allowed_origins=norm, allow_private=allow_private, allowed_schemes=schemes)

    def check_url(self, url: str, *, resolve: bool = True) -> str:
        parts = urlsplit(url)
        if parts.scheme not in self.allowed_schemes:
            raise PolicyError(f"scheme {parts.scheme!r} not allowed", details={"url": _safe(url)})
        if not parts.hostname:
            raise PolicyError("url has no host", details={"url": _safe(url)})
        if parts.username or parts.password:
            raise PolicyError("credentials in url are not allowed", details={"url": _safe(url)})
        origin = _origin(url)
        if origin not in self.allowed_origins:
            raise PolicyError(f"origin {origin} is not allowlisted", details={"origin": origin})
        if not self.allow_private:
            _reject_private_host(parts.hostname, resolve=resolve)
        return origin


def _origin(url: str) -> str:
    p = urlsplit(url if "://" in url else f"https://{url}")
    host = (p.hostname or "").lower()
    port = p.port
    default = {"https": 443, "http": 80}.get(p.scheme)
    return f"{p.scheme}://{host}" if port in (None, default) else f"{p.scheme}://{host}:{port}"


def _safe(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.hostname}{p.path}"


def is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return any(addr in n for n in _BLOCKED_NETWORKS)


def _reject_private_host(host: str, *, resolve: bool) -> None:
    lowered = host.lower().rstrip(".")
    if lowered in {"localhost", "metadata", "metadata.google.internal"} or lowered.endswith(".internal"):
        raise PolicyError("private/metadata host rejected", details={"host": host})
    try:
        ipaddress.ip_address(lowered)
        literal = True
    except ValueError:
        literal = False
    if literal:
        if is_private_ip(lowered):
            raise PolicyError("private/metadata address rejected", details={"host": host})
        return
    if not resolve:
        return
    try:
        infos = socket.getaddrinfo(lowered, None)
    except socket.gaierror as exc:
        raise PolicyError("host does not resolve", details={"host": host}) from exc
    for info in infos:
        ip = info[4][0]
        if is_private_ip(str(ip)):
            raise PolicyError("host resolves to a private/metadata address", details={"host": host})
