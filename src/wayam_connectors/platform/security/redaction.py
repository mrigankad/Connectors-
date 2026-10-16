"""Redact secrets from logs, traces, error details and fixtures."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = re.compile(
    r"(token|secret|password|passwd|pwd|api[_-]?key|authorization|cookie|private[_-]?key|"
    r"client[_-]?secret|refresh[_-]?token|access[_-]?key|session|signature|credential)",
    re.IGNORECASE,
)
# Values that look like credentials even under innocent keys.
SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-=]{10,}"),
    re.compile(r"(?i)(x-amz-signature|sig|signature|sv|se|sp|sr|X-Goog-Signature)=[A-Za-z0-9%._\-]{8,}"),
    re.compile(r"(?i)(password|passwd|pwd)=[^\s&;]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
REDACTED = "[REDACTED]"


def redact_text(text: str) -> str:
    out = text
    for pat in SENSITIVE_VALUE_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Deep-copy `value` replacing anything that looks secret."""
    if _depth > 20:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if isinstance(k, str) and SENSITIVE_KEYS.search(k) else redact(v, _depth=_depth + 1)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: (REDACTED if SENSITIVE_KEYS.search(k) else redact_text(v)) for k, v in headers.items()}
