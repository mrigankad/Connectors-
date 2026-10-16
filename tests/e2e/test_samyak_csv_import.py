"""Product journey: SAMYAK file-to-typed-payment import through the full pipeline (T-36, T-37, T-38, T-40, T-48)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import FIXTURES, TenantFixture
from wayam_connectors.contracts.errors import IntegrityError, PolicyError, UnsupportedOperationError
from wayam_connectors.contracts.identity import Product
from wayam_connectors.platform.connections import ConnectionService
from wayam_connectors.platform.file_import import FileImportService
from wayam_connectors.platform.raw_store import LocalEncryptedRawStore
from wayam_connectors.platform.records import RecordStore

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

PARSE = {
    "mapping": {
        "columns": [
            {"source": "Txn ID", "target": "id", "required": True},
            {"source": "Booking Date", "target": "booking", "type": "date", "date_format": "%Y-%m-%d", "required": True},
            {"source": "Value Date", "target": "value", "type": "date", "date_format": "%Y-%m-%d"},
            {"source": "Amount", "target": "amount", "type": "decimal", "required": True},
            {"source": "Currency", "target": "ccy", "required": True},
            {"source": "Reference", "target": "ref"},
            {"source": "Description", "target": "desc"},
        ],
        "number_locale": {"decimal_separator": ".", "thousands_separator": ","},
        "key_columns": ["id"],
    }
}
MAPPING = {
    "account_id": "HDFC-001",
    "legal_entity": "Wayam India Pvt Ltd",
    "currency": "column:ccy",
    "sign_convention": "signed_amount",
    "amount_column": "amount",
    "booking_date_column": "booking",
    "value_date_column": "value",
    "transaction_id_column": "id",
    "reference_column": "ref",
    "description_column": "desc",
    "statement_id": "STMT-2026-09",
}
TOTALS = {"transaction_count": 4, "total_debits": "5340.50", "total_credits": "1250.01", "net_movement": "-4090.49"}


@pytest.fixture
def svc(store, registry, grants, backend, raw_root):
    return FileImportService(registry, grants, RecordStore(store), LocalEncryptedRawStore(raw_root, backend))


def _setup(store, grants, t: TenantFixture):
    cid = ConnectionService(store).create(t.admin, "files.csv", "bank statements", {})
    grants.grant(t.admin, cid, Product.SAMYAK, ["FileParser"])
    return cid


def test_csv_to_typed_bank_transactions(store, grants, svc, tenant_a, tenant_b, backend, raw_root):
    data = (FIXTURES / "csv" / "bank_statement_signed.csv").read_bytes()
    ca = _setup(store, grants, tenant_a)
    res = svc.import_file(
        tenant_a.product_ctx,
        ca,
        connector_id="files.csv",
        data=data,
        source_ref="sftp://bank/stmt-2026-09.csv",
        parse_options=PARSE,
        domain_mapping="finance.bank_transaction",
        mapping_config=MAPPING,
        control_totals=TOTALS,
    )
    assert res["status"] == "imported" and res["stored"] == 4 and res["quarantined"] == 0
    assert res["summary"]["control_totals"] == "reconciled" and res["summary"]["net_movement"] == "-4090.49"
    rows = RecordStore(store).current_records(tenant_a.tenant_id, ca, "finance.bank_transaction")
    by_id = {r["source_object_id"]: r for r in rows}
    t2 = by_id["T-1002"]["payload"]
    assert t2["amount"] == {"amount": "-340.50", "currency": "INR"} and t2["direction"] == "debit"
    assert t2["booking_date"] == "2026-09-02" and t2["legal_entity"] == "Wayam India Pvt Ltd" and t2["account_id"] == "HDFC-001"
    assert by_id["T-1003"]["payload"]["value_date"] == "2026-09-04" and by_id["T-1004"]["payload"]["amount"]["amount"] == "0.01"
    assert by_id["T-1002"]["provenance"] == "sftp://bank/stmt-2026-09.csv#row=2"
    assert by_id["T-1002"]["mapping_version"] == "finance.bank_transaction.csv/1"
    assert by_id["T-1002"]["raw_object_ref"].startswith("raw://")
    # raw evidence retained, encrypted, and only readable for the right tenant/connection
    raw = LocalEncryptedRawStore(raw_root, backend)
    assert raw.get(tenant_a.tenant_id, ca, res["source_sha256"]) == data
    assert data not in b"".join(p.read_bytes() for p in raw_root.rglob("*") if p.is_file())
    with pytest.raises(PolicyError):
        raw.get(tenant_b.tenant_id, ca, res["source_sha256"])

    # repeat file: detected, not re-imported
    again = svc.import_file(
        tenant_a.product_ctx,
        ca,
        connector_id="files.csv",
        data=data,
        source_ref="x",
        parse_options=PARSE,
        domain_mapping="finance.bank_transaction",
        mapping_config=MAPPING,
    )
    assert again["status"] == "duplicate_input" and again["stored"] == 0
    forced = svc.import_file(
        tenant_a.product_ctx,
        ca,
        connector_id="files.csv",
        data=data,
        source_ref="x",
        parse_options=PARSE,
        domain_mapping="finance.bank_transaction",
        mapping_config=MAPPING,
        allow_duplicate_input=True,
    )
    assert forced["status"] == "imported" and forced["stored"] == 0 and forced["replayed"] == 4

    # T-36: same file from a different tenant is a separate import with separate records
    cb = _setup(store, grants, tenant_b)
    other = svc.import_file(
        tenant_b.product_ctx,
        cb,
        connector_id="files.csv",
        data=data,
        source_ref="x",
        parse_options=PARSE,
        domain_mapping="finance.bank_transaction",
        mapping_config=MAPPING,
    )
    assert other["status"] == "imported" and other["stored"] == 4
    assert len(RecordStore(store).current_records(tenant_b.tenant_id, cb, "finance.bank_transaction")) == 4
    assert RecordStore(store).current_records(tenant_b.tenant_id, ca, "finance.bank_transaction") == []


def test_control_total_mismatch_stores_nothing(store, grants, svc, tenant_a):
    data = (FIXTURES / "csv" / "bank_statement_signed.csv").read_bytes()
    ca = _setup(store, grants, tenant_a)
    with pytest.raises(IntegrityError) as ei:
        svc.import_file(
            tenant_a.product_ctx,
            ca,
            connector_id="files.csv",
            data=data,
            source_ref="x",
            parse_options=PARSE,
            domain_mapping="finance.bank_transaction",
            mapping_config=MAPPING,
            control_totals={**TOTALS, "total_debits": "5340.51"},
        )
    assert ei.value.details["mismatches"]["total_debits"] == {"expected": "5340.51", "actual": "5340.50"}
    assert RecordStore(store).current_records(tenant_a.tenant_id, ca, "finance.bank_transaction") == []
    assert RecordStore(store).get_checkpoint(tenant_a.tenant_id, ca, "finance.bank_transaction", ei.value.details.get("sha")) is None


def test_bad_rows_are_quarantined_with_reasons(store, grants, svc, tenant_a):
    data = (FIXTURES / "csv" / "bank_statement_indicator_dupe.csv").read_bytes()
    ca = _setup(store, grants, tenant_a)
    parse = {
        "mapping": {
            "columns": [
                {"source": "id", "target": "id", "required": True},
                {"source": "date", "target": "booking", "type": "date", "date_format": "%Y-%m-%d", "required": True},
                {"source": "amount", "target": "amount", "type": "decimal", "required": True},
                {"source": "dc", "target": "dc"},
                {"source": "memo", "target": "memo"},
            ],
            "key_columns": ["id"],
        }
    }
    mapping = {
        "account_id": "A",
        "currency": "INR",
        "sign_convention": "amount_with_indicator",
        "amount_column": "amount",
        "indicator_column": "dc",
        "booking_date_column": "booking",
        "transaction_id_column": "id",
        "description_column": "memo",
    }
    res = svc.import_file(
        tenant_a.product_ctx,
        ca,
        connector_id="files.csv",
        data=data,
        source_ref="f",
        parse_options=parse,
        domain_mapping="finance.bank_transaction",
        mapping_config=mapping,
    )
    assert res["stored"] == 1 and res["quarantined"] == 3
    q = {r["provenance"]: r["reason"] for r in RecordStore(store).quarantined(tenant_a.tenant_id, ca)}
    assert q == {"f#row=2&column=amount": "type", "f#row=3": "duplicate_key", "f#row=4": "mapping"}
    stored = RecordStore(store).current_records(tenant_a.tenant_id, ca, "finance.bank_transaction")
    assert stored[0]["payload"]["amount"]["amount"] == "100.00" and Decimal(stored[0]["payload"]["amount"]["amount"]) == Decimal("100")


def test_grant_and_capability_are_enforced(store, grants, svc, tenant_a, tenant_b):
    data = b"a\n1\n"
    ca = ConnectionService(store).create(tenant_a.admin, "files.csv", "no grant", {})
    with pytest.raises(PolicyError):
        svc.import_file(
            tenant_a.product_ctx,
            ca,
            connector_id="files.csv",
            data=data,
            source_ref="f",
            parse_options=PARSE,
            domain_mapping="finance.bank_transaction",
            mapping_config=MAPPING,
        )
    grants.grant(tenant_a.admin, ca, Product.SAMYAK, ["FileParser"])
    with pytest.raises(PolicyError):  # other tenant's product key on this connection id
        svc.import_file(
            tenant_b.product_ctx,
            ca,
            connector_id="files.csv",
            data=data,
            source_ref="f",
            parse_options=PARSE,
            domain_mapping="finance.bank_transaction",
            mapping_config=MAPPING,
        )
    with pytest.raises(UnsupportedOperationError):
        svc.import_file(
            tenant_a.product_ctx,
            ca,
            connector_id="nope.nope",
            data=data,
            source_ref="f",
            parse_options=PARSE,
            domain_mapping="finance.bank_transaction",
            mapping_config=MAPPING,
        )
