from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from wayam_connectors.contracts.domains.finance import DebitCredit
from wayam_connectors.contracts.errors import DataError, IntegrityError
from wayam_connectors.mappings.finance.bank_transactions import (
    BankTransactionMapping,
    ControlTotals,
    SignConvention,
    map_row,
    reconcile,
)

OBS = "2026-10-16T00:00:00+00:00"


def _row(**kw):
    base = {
        "id": "T1",
        "booking": date(2026, 9, 1),
        "amount": Decimal("-10.50"),
        "ccy": "INR",
        "ref": "R",
        "_row": 1,
        "_provenance": "f#row=1",
    }
    base.update(kw)
    return base


def test_signed_amount_debit_sign_preserved():
    m = BankTransactionMapping(
        account_id="ACC-1",
        currency="column:ccy",
        sign_convention=SignConvention.SIGNED_AMOUNT,
        amount_column="amount",
        booking_date_column="booking",
        transaction_id_column="id",
        reference_column="ref",
    )
    rec = map_row(_row(), m, source_system="files.csv", observed_at_iso=OBS)
    assert rec.direction == DebitCredit.DEBIT and rec.amount.amount == Decimal("-10.50") and rec.amount.currency == "INR"
    assert rec.transaction_id == "T1" and rec.account_id == "ACC-1" and rec.reference == "R"
    inv = m.model_copy(update={"sign_convention": SignConvention.SIGNED_AMOUNT_DEBIT_POSITIVE})
    rec2 = map_row(_row(amount=Decimal("10.50")), inv, source_system="files.csv", observed_at_iso=OBS)
    assert rec2.direction == DebitCredit.DEBIT and rec2.amount.amount == Decimal("-10.50")


def test_debit_credit_columns_and_indicator():
    m = BankTransactionMapping(
        account_id="A",
        currency="EUR",
        sign_convention=SignConvention.DEBIT_CREDIT_COLUMNS,
        debit_column="debit",
        credit_column="credit",
        booking_date_column="booking",
        transaction_id_column="id",
    )
    r = map_row(_row(debit=None, credit=Decimal("5")), m, source_system="s", observed_at_iso=OBS)
    assert r.direction == DebitCredit.CREDIT and r.amount.amount == Decimal("5")
    with pytest.raises(DataError, match="exactly one"):
        map_row(_row(debit=Decimal("1"), credit=Decimal("1")), m, source_system="s", observed_at_iso=OBS)
    with pytest.raises(DataError, match="exactly one"):
        map_row(_row(debit=None, credit=None), m, source_system="s", observed_at_iso=OBS)
    ind = BankTransactionMapping(
        account_id="A",
        currency="EUR",
        sign_convention=SignConvention.AMOUNT_WITH_INDICATOR,
        amount_column="amount",
        indicator_column="dc",
        booking_date_column="booking",
        transaction_id_column="id",
    )
    r = map_row(_row(amount=Decimal("7"), dc="dr"), ind, source_system="s", observed_at_iso=OBS)
    assert r.direction == DebitCredit.DEBIT and r.amount.amount == Decimal("-7")
    with pytest.raises(DataError, match="indicator"):
        map_row(_row(amount=Decimal("7"), dc="??"), ind, source_system="s", observed_at_iso=OBS)


def test_missing_fields_are_errors_not_defaults():
    m = BankTransactionMapping(
        account_id="column:acct",
        currency="column:ccy",
        sign_convention=SignConvention.SIGNED_AMOUNT,
        amount_column="amount",
        booking_date_column="booking",
        transaction_id_column="id",
    )
    with pytest.raises(DataError, match="account_id"):
        map_row(_row(acct=None), m, source_system="s", observed_at_iso=OBS)
    with pytest.raises(DataError, match="currency"):
        map_row(_row(acct="A", ccy=None), m, source_system="s", observed_at_iso=OBS)
    with pytest.raises(DataError, match="booking_date"):
        map_row(_row(acct="A", booking=None), m, source_system="s", observed_at_iso=OBS)
    with pytest.raises(DataError, match="zero amount"):
        map_row(_row(acct="A", amount=Decimal("0")), m, source_system="s", observed_at_iso=OBS)
    with pytest.raises(DataError, match="transaction_id"):
        map_row(_row(acct="A", id=""), m, source_system="s", observed_at_iso=OBS)


def test_derived_ids_are_stable_and_distinguish_repeats():
    m = BankTransactionMapping(
        account_id="A",
        currency="INR",
        sign_convention=SignConvention.SIGNED_AMOUNT,
        amount_column="amount",
        booking_date_column="booking",
        id_strategy="derived",
    )
    a = map_row(_row(), m, source_system="s", observed_at_iso=OBS)
    b = map_row(_row(), m, source_system="s", observed_at_iso=OBS)
    c = map_row(_row(), m, source_system="s", observed_at_iso=OBS, occurrence=1)
    assert a.transaction_id == b.transaction_id != c.transaction_id and a.transaction_id.startswith("derived:")


def test_reversal_link_preserved():
    m = BankTransactionMapping(
        account_id="A",
        currency="INR",
        sign_convention=SignConvention.SIGNED_AMOUNT,
        amount_column="amount",
        booking_date_column="booking",
        transaction_id_column="id",
        reversal_of_column="rev",
    )
    r = map_row(_row(rev="T0", amount=Decimal("10.50")), m, source_system="s", observed_at_iso=OBS)
    assert r.is_reversal and r.reversal_of_transaction_id == "T0" and r.direction == DebitCredit.CREDIT


def test_reconcile_control_totals():
    m = BankTransactionMapping(
        account_id="A",
        currency="INR",
        sign_convention=SignConvention.SIGNED_AMOUNT,
        amount_column="amount",
        booking_date_column="booking",
        transaction_id_column="id",
    )
    recs = [
        map_row(_row(id="1", amount=Decimal("100")), m, source_system="s", observed_at_iso=OBS),
        map_row(_row(id="2", amount=Decimal("-40.25")), m, source_system="s", observed_at_iso=OBS),
    ]
    ok = reconcile(
        recs,
        ControlTotals(transaction_count=2, total_debits=Decimal("40.25"), total_credits=Decimal("100"), net_movement=Decimal("59.75")),
        quarantined=0,
    )
    assert ok["control_totals"] == "reconciled"
    with pytest.raises(IntegrityError) as ei:
        reconcile(recs, ControlTotals(total_debits=Decimal("40.26")), quarantined=0)
    assert "total_debits" in ei.value.details["mismatches"]
    with pytest.raises(IntegrityError):
        reconcile(recs, ControlTotals(transaction_count=3), quarantined=0)
    with pytest.raises(IntegrityError, match="reconcile"):
        reconcile(recs, ControlTotals(transaction_count=3), quarantined=1)  # count matches only because a row was quarantined
    assert reconcile(recs, None, quarantined=0)["control_totals"].startswith("not provided")
