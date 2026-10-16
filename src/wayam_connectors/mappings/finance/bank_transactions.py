"""Map parsed CSV rows to finance BankTransaction records.

Sign conventions are declared, never guessed. Money stays Decimal. A row that
cannot be mapped raises DataError (quarantined by the pipeline); the mapping never
fills a missing financial field with a default.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wayam_connectors.contracts.domains.common import Money
from wayam_connectors.contracts.domains.finance import BankTransaction, DebitCredit
from wayam_connectors.contracts.errors import DataError, IntegrityError

MAPPING_VERSION = "finance.bank_transaction.csv/1"


class SignConvention(StrEnum):
    SIGNED_AMOUNT = "signed_amount"  # one column, positive = credit, negative = debit
    SIGNED_AMOUNT_DEBIT_POSITIVE = "signed_amount_debit_positive"  # one column, positive = debit
    DEBIT_CREDIT_COLUMNS = "debit_credit_columns"  # two unsigned columns
    AMOUNT_WITH_INDICATOR = "amount_with_indicator"  # unsigned amount + D/C indicator column


class ControlTotals(BaseModel):
    """Source control totals the import must reconcile against. Any field left None is not checked."""

    model_config = ConfigDict(frozen=True)
    transaction_count: int | None = None
    total_debits: Decimal | None = Field(default=None, description="Unsigned sum of debits")
    total_credits: Decimal | None = Field(default=None, description="Unsigned sum of credits")
    net_movement: Decimal | None = Field(default=None, description="credits - debits")


class BankTransactionMapping(BaseModel):
    model_config = ConfigDict(frozen=True)
    account_id: str = Field(description="Constant account id, or 'column:<target>' to read per row")
    legal_entity: str | None = Field(default=None, description="Constant or 'column:<target>'")
    currency: str = Field(description="ISO 4217 constant, or 'column:<target>'")
    statement_id: str | None = None
    sign_convention: SignConvention
    amount_column: str | None = None
    debit_column: str | None = None
    credit_column: str | None = None
    indicator_column: str | None = None
    debit_indicators: tuple[str, ...] = ("D", "DR", "DEBIT")
    credit_indicators: tuple[str, ...] = ("C", "CR", "CREDIT")
    booking_date_column: str
    value_date_column: str | None = None
    transaction_id_column: str | None = Field(default=None, description="Source id; if absent a deterministic id is derived")
    reference_column: str | None = None
    description_column: str | None = None
    counterparty_name_column: str | None = None
    counterparty_account_column: str | None = None
    reversal_of_column: str | None = None
    id_strategy: Literal["column", "derived"] = "column"

    @model_validator(mode="after")
    def _consistent(self) -> BankTransactionMapping:
        sc = self.sign_convention
        if sc in (SignConvention.SIGNED_AMOUNT, SignConvention.SIGNED_AMOUNT_DEBIT_POSITIVE) and not self.amount_column:
            raise ValueError("amount_column required for signed amount conventions")
        if sc == SignConvention.DEBIT_CREDIT_COLUMNS and not (self.debit_column and self.credit_column):
            raise ValueError("debit_column and credit_column required")
        if sc == SignConvention.AMOUNT_WITH_INDICATOR and not (self.amount_column and self.indicator_column):
            raise ValueError("amount_column and indicator_column required")
        if self.id_strategy == "column" and not self.transaction_id_column:
            raise ValueError("transaction_id_column required unless id_strategy='derived'")
        return self


def _resolve(spec: str | None, row: dict[str, Any]) -> Any:
    if spec is None:
        return None
    if spec.startswith("column:"):
        return row.get(spec[len("column:") :])
    return spec


def _require(row: dict[str, Any], column: str | None, name: str) -> Any:
    if column is None:
        return None
    if column not in row:
        raise DataError(f"mapped column {column!r} ({name}) is not in the parsed row")
    return row[column]


def map_row(
    row: dict[str, Any], mapping: BankTransactionMapping, *, source_system: str, observed_at_iso: str, occurrence: int = 0
) -> BankTransaction:
    booking = _require(row, mapping.booking_date_column, "booking_date")
    if not isinstance(booking, date):
        raise DataError("booking_date is missing or not a date", details={"value": str(booking)})
    value_date = _require(row, mapping.value_date_column, "value_date")
    currency = _resolve(mapping.currency, row)
    if not currency:
        raise DataError("currency is missing")
    account_id = _resolve(mapping.account_id, row)
    if not account_id:
        raise DataError("account_id is missing")

    sc = mapping.sign_convention
    if sc in (SignConvention.SIGNED_AMOUNT, SignConvention.SIGNED_AMOUNT_DEBIT_POSITIVE):
        amt = _require(row, mapping.amount_column, "amount")
        if not isinstance(amt, Decimal):
            raise DataError("amount is missing or not decimal", details={"value": str(amt)})
        if amt == 0:
            raise DataError("zero amount cannot be classified as debit or credit")
        if sc == SignConvention.SIGNED_AMOUNT_DEBIT_POSITIVE:
            amt = -amt
        direction = DebitCredit.CREDIT if amt > 0 else DebitCredit.DEBIT
        signed = amt
    elif sc == SignConvention.DEBIT_CREDIT_COLUMNS:
        debit = _require(row, mapping.debit_column, "debit")
        credit = _require(row, mapping.credit_column, "credit")
        if (debit is None) == (credit is None):
            raise DataError("exactly one of debit/credit must be present", details={"debit": str(debit), "credit": str(credit)})
        if debit is not None:
            if debit <= 0:
                raise DataError("debit column must be positive", details={"debit": str(debit)})
            direction, signed = DebitCredit.DEBIT, -debit
        else:
            if credit <= 0:
                raise DataError("credit column must be positive", details={"credit": str(credit)})
            direction, signed = DebitCredit.CREDIT, credit
    else:
        amt = _require(row, mapping.amount_column, "amount")
        ind = _require(row, mapping.indicator_column, "indicator")
        if not isinstance(amt, Decimal) or amt <= 0:
            raise DataError("amount must be a positive decimal with an indicator", details={"value": str(amt)})
        ind_u = str(ind or "").strip().upper()
        if ind_u in mapping.debit_indicators:
            direction, signed = DebitCredit.DEBIT, -amt
        elif ind_u in mapping.credit_indicators:
            direction, signed = DebitCredit.CREDIT, amt
        else:
            raise DataError("unknown debit/credit indicator", details={"indicator": str(ind)})

    reference = _require(row, mapping.reference_column, "reference")
    description = _require(row, mapping.description_column, "description")
    if mapping.id_strategy == "column":
        tx_id = _require(row, mapping.transaction_id_column, "transaction_id")
        if tx_id in (None, ""):
            raise DataError("transaction_id is missing")
        tx_id = str(tx_id)
    else:
        basis = "|".join([str(account_id), booking.isoformat(), str(signed), str(reference or ""), str(description or ""), str(occurrence)])
        tx_id = "derived:" + hashlib.sha256(basis.encode()).hexdigest()[:32]
    reversal_of = _require(row, mapping.reversal_of_column, "reversal_of")
    return BankTransaction(
        source_system=source_system,
        source_object_id=tx_id,
        observed_at_iso=observed_at_iso,
        transaction_id=tx_id,
        account_id=str(account_id),
        statement_id=mapping.statement_id,
        legal_entity=(str(le) if (le := _resolve(mapping.legal_entity, row)) else None),
        booking_date=booking,
        value_date=value_date if isinstance(value_date, date) else None,
        amount=Money(amount=signed, currency=str(currency)),
        direction=direction,
        counterparty_name=_require(row, mapping.counterparty_name_column, "counterparty_name"),
        counterparty_account=_require(row, mapping.counterparty_account_column, "counterparty_account"),
        reference=str(reference) if reference is not None else None,
        description=str(description) if description is not None else None,
        reversal_of_transaction_id=str(reversal_of) if reversal_of else None,
        is_reversal=bool(reversal_of),
    )


def reconcile(records: list[BankTransaction], totals: ControlTotals | None, *, quarantined: int) -> dict[str, Any]:
    """Compare mapped records with source control totals. Raises IntegrityError on any mismatch."""
    debits = sum((-r.amount.amount for r in records if r.direction == DebitCredit.DEBIT), Decimal(0))
    credits = sum((r.amount.amount for r in records if r.direction == DebitCredit.CREDIT), Decimal(0))
    currencies = Counter(r.amount.currency for r in records)
    summary = {
        "count": len(records),
        "quarantined": quarantined,
        "total_debits": str(debits),
        "total_credits": str(credits),
        "net_movement": str(credits - debits),
        "currencies": dict(currencies),
    }
    if totals is None:
        summary["control_totals"] = "not provided; not reconciled"
        return summary
    mismatches: dict[str, Any] = {}
    if totals.transaction_count is not None and totals.transaction_count != len(records) + quarantined:
        mismatches["transaction_count"] = {"expected": totals.transaction_count, "mapped": len(records), "quarantined": quarantined}
    if totals.total_debits is not None and totals.total_debits != debits:
        mismatches["total_debits"] = {"expected": str(totals.total_debits), "actual": str(debits)}
    if totals.total_credits is not None and totals.total_credits != credits:
        mismatches["total_credits"] = {"expected": str(totals.total_credits), "actual": str(credits)}
    if totals.net_movement is not None and totals.net_movement != credits - debits:
        mismatches["net_movement"] = {"expected": str(totals.net_movement), "actual": str(credits - debits)}
    if quarantined and not mismatches:
        mismatches["quarantined_rows"] = {"count": quarantined, "note": "totals only reconcile when every row maps"}
    if mismatches:
        raise IntegrityError("control totals do not reconcile", details={"mismatches": mismatches, **summary})
    summary["control_totals"] = "reconciled"
    return summary
