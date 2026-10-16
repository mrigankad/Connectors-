# Provider contract: CSV / TSV (`files.csv`, WCN-283)

Verified: 2026-10-16, locally. Source: Python 3.13 `csv` module (stdlib; the docs link in the catalog is
https://docs.python.org/3/library/csv.html, not retrieved in session, behaviour verified by tests).

| Item | Contract |
|---|---|
| Identity | Inherited: bytes arrive already authorized (upload, SFTP, S3...). No provider login. |
| Input | Any single-character delimiter, declared encoding (UTF-8 BOM stripped), optional header, `skip_rows`. |
| Types | string, integer, decimal, date, datetime, boolean; number locale and date formats are declared, never guessed. |
| Provenance | `<source_ref>#row=<n>` (1-based data rows, blank lines skipped); result carries input sha256. |
| Errors | Encoding, missing mapped columns, malformed quoting, too many rows → `ValidationError` for the whole file. Per-cell type errors and duplicate keys → `ParseIssue`s; the row is excluded and reported, the file still imports. |
| Limits | `FileLimits.max_bytes` (50 MB default), `max_rows` (1 M default), 1 MB per field. |
| Not supported | Format sniffing, multi-table files, formulas (export helper `neutralise_formula` only). |

Tests: `tests/unit/test_csv_parser.py`, `tests/e2e/test_samyak_csv_import.py`.
