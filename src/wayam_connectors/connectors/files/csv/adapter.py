"""CSV/TSV format adapter.

Bytes in (already authorized by the caller), typed rows out. The adapter is explicit
about everything a customer must decide: delimiter, encoding, header row, column
mapping, locale for numbers and dates. It never guesses a money format. Every row
carries a deterministic provenance (`<source_ref>#row=<n>`, 1-based data rows) and
the result carries the sha256 of the input so repeat imports are detected upstream.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wayam_connectors.contracts.capabilities import ParseIssue, ParseResult
from wayam_connectors.contracts.errors import ValidationError
from wayam_connectors.platform.security.files import DEFAULT_LIMITS, FileLimits, check_size

ADAPTER_VERSION = "0.1.0"
CONNECTOR_ID = "files.csv"
_MAX_FIELD = 1_000_000
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


class ColumnType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"


class NumberLocale(BaseModel):
    """How numbers are written in the file. Defaults are explicit, not inferred."""

    model_config = ConfigDict(frozen=True)
    decimal_separator: Literal[".", ","] = "."
    thousands_separator: Literal[",", ".", " ", "'", ""] = ""
    negative_style: Literal["minus", "parentheses", "trailing_minus", "any"] = "minus"
    currency_symbols: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _distinct(self) -> NumberLocale:
        if self.thousands_separator and self.thousands_separator == self.decimal_separator:
            raise ValueError("decimal and thousands separators must differ")
        return self


class ColumnSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str = Field(description="Header name (or 1-based index as a string when header=False)")
    target: str
    type: ColumnType = ColumnType.STRING
    required: bool = False
    date_format: str | None = Field(default=None, description="strptime format for date/datetime columns")
    true_values: tuple[str, ...] = ("true", "1", "yes", "y")
    false_values: tuple[str, ...] = ("false", "0", "no", "n")
    max_length: int | None = None
    trim: bool = True


class CsvMapping(BaseModel):
    model_config = ConfigDict(frozen=True)
    delimiter: str = ","
    encoding: str = "utf-8"
    has_header: bool = True
    skip_rows: int = 0
    quotechar: str = '"'
    columns: list[ColumnSpec]
    number_locale: NumberLocale = Field(default_factory=NumberLocale)
    key_columns: list[str] = Field(default_factory=list, description="Targets that identify a row; duplicates within a file are reported")
    allow_extra_columns: bool = True
    max_rows: int = 1_000_000

    @model_validator(mode="after")
    def _targets_unique(self) -> CsvMapping:
        targets = [c.target for c in self.columns]
        if len(targets) != len(set(targets)):
            raise ValueError("duplicate target names in mapping")
        if len(self.delimiter) != 1:
            raise ValueError("delimiter must be one character")
        unknown = set(self.key_columns) - set(targets)
        if unknown:
            raise ValueError(f"key_columns not in mapping: {sorted(unknown)}")
        return self


class CsvParser:
    CONNECTOR_ID = CONNECTOR_ID
    ADAPTER_VERSION = ADAPTER_VERSION

    def __init__(self, limits: FileLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def parse(self, data: bytes, *, source_ref: str, options: Mapping[str, Any] | None = None) -> ParseResult:
        if not options or "mapping" not in options:
            raise ValidationError("CSV parsing needs an explicit `mapping` (CsvMapping); nothing is guessed")
        mapping = options["mapping"] if isinstance(options["mapping"], CsvMapping) else CsvMapping.model_validate(options["mapping"])
        check_size(data, self.limits)
        sha = hashlib.sha256(data).hexdigest()
        try:
            text = data.decode(mapping.encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            raise ValidationError(
                "file is not valid in the declared encoding", details={"encoding": mapping.encoding, "error": str(exc)[:200]}
            ) from exc
        if text.startswith("﻿"):
            text = text[1:]
        csv.field_size_limit(_MAX_FIELD)
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=mapping.delimiter, quotechar=mapping.quotechar, strict=True)
        issues: list[ParseIssue] = []
        items: list[dict[str, Any]] = []
        seen_keys: dict[tuple[Any, ...], int] = {}
        header_index: dict[str, int] = {}
        physical_line = 0
        data_row = 0
        try:
            for raw in reader:
                physical_line += 1
                if physical_line <= mapping.skip_rows:
                    continue
                if mapping.has_header and not header_index:
                    header_index = {h.strip(): i for i, h in enumerate(raw)}
                    missing = [c.source for c in mapping.columns if c.source not in header_index]
                    if missing:
                        raise ValidationError(
                            "mapped columns missing from header", details={"missing": missing, "header": list(header_index)}
                        )
                    if not mapping.allow_extra_columns:
                        extra = sorted(set(header_index) - {c.source for c in mapping.columns})
                        if extra:
                            raise ValidationError("unexpected columns in header", details={"extra": extra})
                    continue
                if not any(cell.strip() for cell in raw):
                    continue  # blank line
                data_row += 1
                if data_row > mapping.max_rows:
                    raise ValidationError("file has more rows than allowed", details={"max_rows": mapping.max_rows})
                location = f"{source_ref}#row={data_row}"
                record: dict[str, Any] = {}
                row_ok = True
                for col in mapping.columns:
                    idx = header_index[col.source] if mapping.has_header else int(col.source) - 1
                    cell = raw[idx] if idx < len(raw) else ""
                    try:
                        record[col.target] = _convert(cell, col, mapping.number_locale)
                    except ValueError as exc:
                        issues.append(ParseIssue(location=f"{location}&column={col.source}", code="type", message=str(exc)))
                        row_ok = False
                if not row_ok:
                    continue
                if mapping.key_columns:
                    key = tuple(record[k] for k in mapping.key_columns)
                    if key in seen_keys:
                        issues.append(
                            ParseIssue(
                                location=location,
                                code="duplicate_key",
                                message=f"duplicate of row {seen_keys[key]} on {mapping.key_columns}",
                            )
                        )
                        continue
                    seen_keys[key] = data_row
                record["_row"] = data_row
                record["_provenance"] = location
                items.append(record)
        except csv.Error as exc:
            raise ValidationError("malformed CSV", details={"line": physical_line, "error": str(exc)[:200]}) from exc
        if mapping.has_header and not header_index:
            raise ValidationError("file has no header row")
        return ParseResult(
            parser=CONNECTOR_ID,
            parser_version=ADAPTER_VERSION,
            source_sha256=sha,
            items=items,
            issues=issues,
            complete=not issues,
            stats={"data_rows": data_row, "accepted": len(items), "rejected": len(issues), "bytes": len(data)},
        )


def _convert(cell: str, col: ColumnSpec, locale: NumberLocale) -> Any:
    value = cell.strip() if col.trim else cell
    if value == "":
        if col.required:
            raise ValueError(f"{col.target} is required")
        return None
    if col.max_length is not None and len(value) > col.max_length:
        raise ValueError(f"{col.target} longer than {col.max_length}")
    match col.type:
        case ColumnType.STRING:
            return value
        case ColumnType.INTEGER:
            d = parse_decimal(value, locale)
            if d != d.to_integral_value():
                raise ValueError(f"{col.target} is not an integer: {value!r}")
            return int(d)
        case ColumnType.DECIMAL:
            return parse_decimal(value, locale)
        case ColumnType.DATE:
            return parse_date(value, col.date_format, col.target)
        case ColumnType.DATETIME:
            if not col.date_format:
                raise ValueError(f"{col.target}: datetime columns need date_format")
            try:
                return datetime.strptime(value, col.date_format)  # noqa: DTZ007 - naive by declared format
            except ValueError as exc:
                raise ValueError(f"{col.target}: {value!r} does not match {col.date_format}") from exc
        case ColumnType.BOOLEAN:
            low = value.lower()
            if low in col.true_values:
                return True
            if low in col.false_values:
                return False
            raise ValueError(f"{col.target}: {value!r} is not a boolean")
    raise ValueError(f"unsupported type {col.type}")


_PAREN = re.compile(r"^\((.+)\)$")


def parse_decimal(value: str, locale: NumberLocale) -> Decimal:
    v = value.strip()
    for sym in locale.currency_symbols:
        v = v.replace(sym, "")
    v = v.replace(" ", " ").strip()
    negative = False
    if locale.negative_style in ("parentheses", "any"):
        m = _PAREN.match(v)
        if m:
            negative, v = True, m.group(1).strip()
    if locale.negative_style in ("trailing_minus", "any") and v.endswith("-"):
        negative, v = True, v[:-1].strip()
    if v.startswith("-"):
        if locale.negative_style not in ("minus", "any"):
            raise ValueError(f"leading minus not allowed by locale: {value!r}")
        negative, v = True, v[1:].strip()
    if v.startswith("+"):
        v = v[1:]
    if locale.thousands_separator:
        v = v.replace(locale.thousands_separator, "")
    if locale.decimal_separator == ",":
        if "." in v:
            raise ValueError(f"unexpected '.' in number with ',' decimal separator: {value!r}")
        v = v.replace(",", ".")
    elif "," in v:
        raise ValueError(f"unexpected ',' in number: {value!r}")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", v):
        raise ValueError(f"not a number: {value!r}")
    try:
        d = Decimal(v)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    return -d if negative else d


def parse_date(value: str, fmt: str | None, name: str) -> date:
    if not fmt:
        raise ValueError(f"{name}: date columns need an explicit date_format")
    try:
        return datetime.strptime(value, fmt).date()  # noqa: DTZ007
    except ValueError as exc:
        raise ValueError(f"{name}: {value!r} does not match {fmt}") from exc


def neutralise_formula(value: str) -> str:
    """For CSV *export*: prefix cells that spreadsheet apps would evaluate (CSV injection)."""
    return "'" + value if value.startswith(_FORMULA_PREFIX) else value


ADAPTER = CsvParser
