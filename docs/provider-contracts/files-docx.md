# Provider contract: Word DOCX parser (`files.docx`, WCN-285)

Verified: 2026-10-16 with python-docx 1.2.0 on python-docx-generated fixtures (`fixtures/office/*.docx`, built by
`scripts/build_office_fixtures.py`). Word-authored samples are still required before `sandbox_verified`.

| Item | Contract |
|---|---|
| Input | OOXML `.docx` only; container inspected first (traversal, bombs, `vbaProject.bin`/ActiveX, DOCTYPE/ENTITY). `.doc`/`.docm` rejected. |
| Output | Fragments in body order: paragraphs (`paragraph_index` over all body paragraphs, empty ones keep their index) and table cells (`table_index,row,column`; merged cells once). Style name reported. |
| Completeness | Images or text boxes present → every fragment `incomplete` with reason; footnotes present → issue `not_extracted`. |
| Not extracted | Headers/footers, footnotes, comments, text boxes, SmartArt, images (no OCR). |

Tests: `tests/unit/test_office_parsers.py`.
