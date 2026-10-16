# ADR-0005: Dependency choices for the first batch

Date: 2026-10-16. Status: accepted.

* psycopg 3 (LGPL-3.0, binary wheel) for both platform state and the PostgreSQL connector. SQLAlchemy is not
  needed: every query is explicit SQL with server-side parameters.
* python-docx / python-pptx (MIT) for OOXML parsing, behind our own container inspection. Docling/MarkItDown/
  Unstructured remain candidates for PDF/OCR (P1); none is pulled in until a slice needs it.
* No dlt, Singer, Airbyte, Nango, n8n, Activepieces, Composio or Pipedream code is imported. The license findings
  in `catalog/research/Wayam_Open_Source_Options.csv` (PyAirbyte ELv2, Nango ELv2, n8n Sustainable Use, Pipedream
  source-available) are recorded in `docs/license-register.json` as evaluated-not-adopted.
* Dependencies are pinned exactly in `pyproject.toml` and locked in `uv.lock`. Dependabot config
  (`.github/dependabot.yml`) opens update PRs; the offline CI lane is the contract test for them.
