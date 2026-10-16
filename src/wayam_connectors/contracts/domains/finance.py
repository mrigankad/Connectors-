"""Finance domain. Preserves sign, original amount, booking/value dates, reversals, legal entity.
Matching and journal approval belong to SAMYAK, not here."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import Field

from .common import DomainRecord, Money


class DebitCredit(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class BankAccount(DomainRecord):
    account_id: str
    legal_entity: str | None = None
    iban: str | None = None
    account_number_masked: str | None = None
    currency: str
    bank_name: str | None = None


class Statement(DomainRecord):
    statement_id: str
    account_id: str
    legal_entity: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    opening_balance: Money | None = None
    closing_balance: Money | None = None
    transaction_count: int | None = Field(default=None, description="Source control total if provided")
    control_total_debits: Money | None = None
    control_total_credits: Money | None = None


class BankTransaction(DomainRecord):
    transaction_id: str
    account_id: str
    statement_id: str | None = None
    legal_entity: str | None = None
    booking_date: date
    value_date: date | None = None
    amount: Money = Field(description="Signed: positive credit, negative debit, as the source sign convention maps")
    original_amount: Money | None = Field(default=None, description="Amount in original currency when converted")
    direction: DebitCredit
    counterparty_name: str | None = None
    counterparty_account: str | None = None
    reference: str | None = None
    description: str | None = None
    reversal_of_transaction_id: str | None = None
    is_reversal: bool = False


class LedgerAccount(DomainRecord):
    account_code: str
    name: str
    legal_entity: str | None = None
    account_type: str | None = None
    currency: str | None = None


class JournalLine(DomainRecord):
    journal_id: str
    line_number: int
    account_code: str
    legal_entity: str | None = None
    posting_date: date
    amount: Money
    direction: DebitCredit
    description: str | None = None
    reversal_of_journal_id: str | None = None


class Counterparty(DomainRecord):
    counterparty_id: str
    name: str
    tax_id: str | None = None
    country: str | None = None


class Invoice(DomainRecord):
    invoice_id: str
    counterparty_id: str | None = None
    legal_entity: str | None = None
    issue_date: date
    due_date: date | None = None
    total: Money
    outstanding: Money | None = None
    status: str | None = None


class Payment(DomainRecord):
    payment_id: str
    counterparty_id: str | None = None
    legal_entity: str | None = None
    payment_date: date
    amount: Money
    direction: DebitCredit
    reference: str | None = None
    invoice_ids: list[str] = Field(default_factory=list)


class Settlement(DomainRecord):
    settlement_id: str
    provider: str
    settlement_date: date
    gross: Money
    fees: Money | None = None
    net: Money
    payment_ids: list[str] = Field(default_factory=list)
