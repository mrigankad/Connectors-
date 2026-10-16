# Provider contract: PowerPoint PPTX parser (`files.pptx`, WCN-286)

Verified: 2026-10-16 with python-pptx 1.0.2 on generated fixtures (`fixtures/office/deck.pptx`). PowerPoint-authored
samples are still required before `sandbox_verified`.

| Item | Contract |
|---|---|
| Input | OOXML `.pptx` only; same container inspection as DOCX. `.ppt`/`.pptm` rejected. |
| Output | Per slide (1-based): text frames (`kind=slide`, `shape_id`), table cells (`kind=table_cell`, row/column, spanned cells skipped), speaker notes (`kind=notes`). Group shapes are walked. |
| Completeness | A slide with pictures and no text → one `incomplete` fragment with empty text and reason "image-only slide; OCR not implemented"; result `complete=false`. |
| Not extracted | Charts, SmartArt, embedded objects, masters/layouts, comments; no OCR; no visual reading order. |

Tests: `tests/unit/test_office_parsers.py`.
