"""Shared fixtures. Integration tests need a real local PostgreSQL (WAYAM_TEST_DATABASE_URL)."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from wayam_connectors.contracts.identity import Product, TenantContext
from wayam_connectors.platform.credentials import CredentialStore, LocalSecretBackend
from wayam_connectors.platform.db import Store
from wayam_connectors.platform.registry import load_registry, sync_definitions
from wayam_connectors.platform.tenancy import GrantService, TenantService

TEST_DSN = os.environ.get("WAYAM_TEST_DATABASE_URL", "postgresql://wayam:wayam@127.0.0.1:5432/wayam_connectors_test")
TEST_KEY = base64.b64encode(b"\x01" * 32).decode()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _db_available() -> bool:
    try:
        with psycopg.connect(TEST_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def store() -> Store:
    if not _db_available():
        pytest.skip(f"BLOCKED: no PostgreSQL at {TEST_DSN}; integration tests not run")
    s = Store(TEST_DSN)
    s.reset_for_tests()
    s.migrate()
    sync_definitions(s, load_registry())
    return s


@pytest.fixture(scope="session")
def registry():
    return load_registry()


@pytest.fixture
def backend() -> LocalSecretBackend:
    return LocalSecretBackend(TEST_KEY)


@pytest.fixture
def credentials(store: Store, backend: LocalSecretBackend) -> CredentialStore:
    return CredentialStore(store, backend)


@pytest.fixture
def tenants(store: Store) -> TenantService:
    return TenantService(store)


@pytest.fixture
def grants(store: Store) -> GrantService:
    return GrantService(store)


class TenantFixture:
    def __init__(self, tenant_id: UUID, admin: TenantContext, product_ctx: TenantContext, raw_key: str) -> None:
        self.tenant_id = tenant_id
        self.admin = admin
        self.product_ctx = product_ctx
        self.raw_key = raw_key


def make_tenant(tenants: TenantService, name: str, product: Product = Product.SAMYAK) -> TenantFixture:
    tid = tenants.create_tenant(name)
    admin_key = tenants.issue_api_key(tid, Product.PLATFORM, roles=["admin"])
    product_key = tenants.issue_api_key(tid, product)
    admin = tenants.authenticate(admin_key)
    product_ctx = tenants.authenticate(product_key)
    return TenantFixture(tid, admin, product_ctx, product_key)


@pytest.fixture
def tenant_a(tenants: TenantService) -> TenantFixture:
    return make_tenant(tenants, "tenant-a")


@pytest.fixture
def tenant_b(tenants: TenantService) -> TenantFixture:
    return make_tenant(tenants, "tenant-b")


@pytest.fixture
def raw_root(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path / "raw"
