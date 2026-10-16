"""Typed connector errors.

Every failure an adapter or the runtime raises is one of these classes so that
the job runner can decide, without string matching, whether to retry, block the
connection, quarantine a record or dead-letter the job.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorClass(StrEnum):
    """How the runtime should react to the error."""

    RETRYABLE = "retryable"  # transport/5xx/timeout: retry with backoff
    RATE_LIMITED = "rate_limited"  # 429 or provider quota: honour Retry-After
    AUTH = "auth"  # invalid/expired/revoked credentials: block the connection, no retry
    SCOPE = "scope"  # credentials valid but lack permission: block, no retry
    UNSUPPORTED = "unsupported"  # operation/capability not declared or not available
    VALIDATION = "validation"  # caller input invalid: fail fast
    DATA = "data"  # source data cannot be mapped: quarantine record, continue
    CURSOR_EXPIRED = "cursor_expired"  # delta token gone: documented resync required
    POLICY = "policy"  # blocked by tenant/product/security policy
    INTEGRITY = "integrity"  # counts/control totals disagree: job fails, nothing invented
    INTERNAL = "internal"


class ConnectorError(Exception):
    """Base class for every typed error. `details` must never contain secrets."""

    error_class: ErrorClass = ErrorClass.INTERNAL
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "class": self.error_class.value,
            "retryable": self.retryable,
            "message": self.message,
            "details": self.details,
        }


class RetryableError(ConnectorError):
    error_class = ErrorClass.RETRYABLE
    retryable = True


class RateLimitedError(ConnectorError):
    error_class = ErrorClass.RATE_LIMITED
    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after_seconds = retry_after_seconds
        if retry_after_seconds is not None:
            self.details["retry_after_seconds"] = retry_after_seconds


class AuthError(ConnectorError):
    error_class = ErrorClass.AUTH


class ScopeError(ConnectorError):
    error_class = ErrorClass.SCOPE


class UnsupportedOperationError(ConnectorError):
    """The connector does not declare this capability/operation. Never a silent no-op."""

    error_class = ErrorClass.UNSUPPORTED


class ValidationError(ConnectorError):
    error_class = ErrorClass.VALIDATION


class DataError(ConnectorError):
    """A single record failed mapping; the pipeline quarantines it and continues."""

    error_class = ErrorClass.DATA


class CursorExpiredError(ConnectorError):
    error_class = ErrorClass.CURSOR_EXPIRED


class PolicyError(ConnectorError):
    error_class = ErrorClass.POLICY


class IntegrityError(ConnectorError):
    error_class = ErrorClass.INTEGRITY


class NotConfiguredError(ConnectorError):
    """A backend/credential needed for a real call is not configured. Not a success."""

    error_class = ErrorClass.POLICY
