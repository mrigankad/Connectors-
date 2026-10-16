from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from wayam_connectors.contracts.capabilities import Capability
from wayam_connectors.contracts.domains.common import Money
from wayam_connectors.contracts.errors import AuthError, RateLimitedError, UnsupportedOperationError
from wayam_connectors.contracts.manifest import ConnectorManifest
from wayam_connectors.platform.registry import check_adapter_conformance, load_registry, validate_against_catalog


def test_money_rejects_float_and_keeps_decimal():
    with pytest.raises(PydanticValidationError):
        Money(amount=1.1, currency="INR")  # type: ignore[arg-type]
    m = Money(amount="1250.00", currency="INR")
    assert m.amount == Decimal("1250.00")
    assert m.minor_units == 125000
    assert Money(amount="1000", currency="JPY").minor_units == 1000
    with pytest.raises(PydanticValidationError):
        Money(amount="1", currency="inr")


def test_error_classes_drive_retry_decisions():
    assert RateLimitedError("x", retry_after_seconds=3).retryable
    assert RateLimitedError("x", retry_after_seconds=3).to_dict()["details"]["retry_after_seconds"] == 3
    assert not AuthError("bad").retryable
    assert UnsupportedOperationError("no").error_class.value == "unsupported"


def test_manifest_rejects_operation_with_undeclared_capability(registry):
    base = registry["files.csv"].manifest.model_dump(mode="json")
    base["operations"].append({"name": "sync", "capability": "RecordSync", "description": "x"})
    with pytest.raises(PydanticValidationError):
        ConnectorManifest.model_validate(base)


def test_adapter_conformance_catches_hidden_methods(registry):
    manifest = registry["files.csv"].manifest

    class Sneaky:
        CONNECTOR_ID = "files.csv"
        ADAPTER_VERSION = manifest.adapter_version

        def parse(self, data, *, source_ref, options=None): ...
        async def perform_action(self, request): ...

    problems = check_adapter_conformance(manifest, Sneaky)
    assert any("undeclared capability ReviewedAction" in p for p in problems)


def test_registry_loads_and_matches_catalog(registry):
    assert "files.csv" in registry
    assert Capability.FILE_PARSER in registry["files.csv"].manifest.capabilities
    assert validate_against_catalog(registry) == []
    assert load_registry(strict=True)
