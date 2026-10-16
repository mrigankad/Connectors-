from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tests.conftest import FIXTURES
from wayam_connectors.connectors.files.csv.adapter import (
    ColumnSpec,
    CsvMapping,
    CsvParser,
    NumberLocale,
    neutralise_formula,
    parse_decimal,
)
from wayam_connectors.contracts.errors import ValidationError

SIGNED = CsvMapping(
    columns=[
        ColumnSpec(source="Txn ID", target="id", required=True),
        ColumnSpec(source="Booking Date", target="booking", type="date", date_format="%Y-%m-%d", required=True),
        ColumnSpec(source="Value Date", target="value", type="date", date_format="%Y-%m-%d"),
        ColumnSpec(source="Amount", target="amount", type="decimal", required=True),
        ColumnSpec(source="Currency", target="ccy", required=True),
        ColumnSpec(source="Reference", target="ref"),
        ColumnSpec(source="Description", target="desc"),
    ],
    number_locale=NumberLocale(decimal_separator=".", thousands_separator=","),
    key_columns=["id"],
)


def test_parse_signed_statement_with_provenance_and_hash():
    data = (FIXTURES / "csv" / "bank_statement_signed.csv").read_bytes()
    res = CsvParser().parse(data, source_ref="upload://stmt.csv", options={"mapping": SIGNED})
    assert res.parser == "files.csv" and len(res.source_sha256) == 64
    assert res.complete and res.stats["data_rows"] == 4 and len(res.items) == 4
    r = res.items[1]
    assert r["amount"] == Decimal("-340.50") and r["booking"] == date(2026, 9, 2)
    assert r["_provenance"] == "upload://stmt.csv#row=2" and r["_row"] == 2
    assert res.items[3]["ref"] is None  # empty optional stays None, not ""


def test_eu_locale_semicolon_and_blank_cells():
    mapping = CsvMapping(
        delimiter=";",
        columns=[
            ColumnSpec(source="ID", target="id", required=True),
            ColumnSpec(source="Buchung", target="booking", type="date", date_format="%d.%m.%Y", required=True),
            ColumnSpec(source="Soll", target="debit", type="decimal"),
            ColumnSpec(source="Haben", target="credit", type="decimal"),
            ColumnSpec(source="Verwendungszweck", target="memo"),
        ],
        number_locale=NumberLocale(decimal_separator=",", thousands_separator="."),
    )
    res = CsvParser().parse((FIXTURES / "csv" / "bank_statement_dc_eu.csv").read_bytes(), source_ref="f", options={"mapping": mapping})
    assert res.items[0]["credit"] == Decimal("1250.00") and res.items[0]["debit"] is None
    assert res.items[2]["debit"] == Decimal("5000.00")
    assert res.items[1]["memo"] == "Gebühren"


def test_type_errors_and_duplicate_keys_are_reported_not_fixed():
    mapping = CsvMapping(
        columns=[
            ColumnSpec(source="id", target="id", required=True),
            ColumnSpec(source="date", target="d", type="date", date_format="%Y-%m-%d"),
            ColumnSpec(source="amount", target="amount", type="decimal", required=True),
            ColumnSpec(source="dc", target="dc"),
        ],
        key_columns=["id"],
    )
    res = CsvParser().parse(
        (FIXTURES / "csv" / "bank_statement_indicator_dupe.csv").read_bytes(), source_ref="f", options={"mapping": mapping}
    )
    codes = {(i.location, i.code) for i in res.issues}
    assert ("f#row=2&column=amount", "type") in codes
    assert ("f#row=3", "duplicate_key") in codes
    assert not res.complete and [i["id"] for i in res.items] == ["X1", "X3"]


def test_hard_failures():
    p = CsvParser()
    with pytest.raises(ValidationError, match="explicit"):
        p.parse(b"a,b\n1,2\n", source_ref="f")
    with pytest.raises(ValidationError, match="empty"):
        p.parse(b"", source_ref="f", options={"mapping": SIGNED})
    with pytest.raises(ValidationError, match="missing from header"):
        p.parse(b"a,b\n1,2\n", source_ref="f", options={"mapping": SIGNED})
    bad_enc = CsvMapping(columns=[ColumnSpec(source="a", target="a")], encoding="utf-8")
    with pytest.raises(ValidationError, match="encoding"):
        p.parse(b"a\n\xff\xfe\n", source_ref="f", options={"mapping": bad_enc})
    with pytest.raises(ValidationError, match="malformed"):
        p.parse(b'a\n"unterminated\n', source_ref="f", options={"mapping": bad_enc})
    with pytest.raises(ValidationError, match="more rows"):
        p.parse(b"a\n1\n2\n", source_ref="f", options={"mapping": bad_enc.model_copy(update={"max_rows": 1})})
    with pytest.raises(ValidationError, match="unexpected columns"):
        p.parse(b"a,b\n1,2\n", source_ref="f", options={"mapping": bad_enc.model_copy(update={"allow_extra_columns": False})})


@pytest.mark.parametrize(
    ("value", "locale", "expected"),
    [
        ("1,234.56", NumberLocale(thousands_separator=","), "1234.56"),
        ("1.234,56", NumberLocale(decimal_separator=",", thousands_separator="."), "1234.56"),
        ("(12.00)", NumberLocale(negative_style="parentheses"), "-12.00"),
        ("12.00-", NumberLocale(negative_style="trailing_minus"), "-12.00"),
        ("₹ 1,00,000.00", NumberLocale(thousands_separator=",", currency_symbols=("₹",)), "100000.00"),
        ("-0.5", NumberLocale(), "-0.5"),
    ],
)
def test_parse_decimal_locales(value, locale, expected):
    assert parse_decimal(value, locale) == Decimal(expected)


@pytest.mark.parametrize(
    "value,locale",
    [
        ("(12.00)", NumberLocale()),
        ("1,5", NumberLocale()),
        ("1.5", NumberLocale(decimal_separator=",")),
        ("abc", NumberLocale()),
        ("1e5", NumberLocale()),
    ],
)
def test_parse_decimal_rejects(value, locale):
    with pytest.raises(ValueError):
        parse_decimal(value, locale)


def test_formula_neutralisation_for_export():
    assert neutralise_formula("=SUM(A1)") == "'=SUM(A1)"
    assert neutralise_formula("plain") == "plain"
