"""Fixture tests for files.docx / files.pptx (T-32, T-33, T-34, T-35)."""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from tests.conftest import FIXTURES
from wayam_connectors.connectors.files.docx.adapter import DocxParser
from wayam_connectors.connectors.files.pptx.adapter import PptxParser
from wayam_connectors.contracts.errors import PolicyError, ValidationError

OFFICE = FIXTURES / "office"


def test_docx_paragraphs_and_tables_with_anchors():
    res = DocxParser().parse((OFFICE / "review.docx").read_bytes(), source_ref="sp://site/review.docx")
    assert res.parser == "files.docx" and res.complete and res.issues == []
    texts = [(i["text"], i["citation"]) for i in res.items]
    assert texts == [
        ("Quarterly Review", "paragraph 0"),
        ("Revenue grew 12% quarter over quarter.", "paragraph 1"),
        ("Metric", "table 0, row 0, column 0"),
        ("Value", "table 0, row 0, column 1"),
        ("Churn", "table 0, row 1, column 0"),
        ("2.1%", "table 0, row 1, column 1"),
        ("Appendix follows.", "paragraph 3"),  # the empty paragraph 2 keeps its index
    ]
    assert res.items[0]["style"] == "Heading 1"
    assert res.items[0]["anchor"] == {"kind": "paragraph", "paragraph_index": 0, "char_start": 0, "char_end": 16}
    assert [i["fragment_index"] for i in res.items] == list(range(7))
    assert res.stats == {"paragraphs": 4, "tables": 1, "fragments": 7, "chars": 91, "images": False}
    again = DocxParser().parse((OFFICE / "review.docx").read_bytes(), source_ref="other")
    assert again.items == res.items and again.source_sha256 == res.source_sha256


def test_docx_with_image_is_marked_incomplete():
    res = DocxParser().parse((OFFICE / "with_image.docx").read_bytes(), source_ref="f")
    assert not res.complete and res.issues[0].code == "incomplete" and "OCR" in res.issues[0].message
    assert res.items[0]["completeness"] == "incomplete" and res.stats["images"] is True


def test_pptx_slides_tables_notes_and_image_only_slide():
    res = PptxParser().parse((OFFICE / "deck.pptx").read_bytes(), source_ref="sp://site/deck.pptx")
    assert res.parser == "files.pptx" and res.stats["slides"] == 3
    slide1_texts = sorted(i["text"] for i in res.items if i["anchor"].get("slide") == 1 and i["anchor"]["kind"] == "slide")
    assert slide1_texts == ["Launch SAMYAK pilot", "Roadmap 2027"]
    notes = [i for i in res.items if i["anchor"]["kind"] == "notes"]
    assert len(notes) == 1 and notes[0]["text"] == "Speaker: mention the bank partner." and notes[0]["citation"] == "slide 1"
    cells = {i["citation"]: i["text"] for i in res.items if i["anchor"]["kind"] == "table_cell"}
    assert cells["slide 2, row 1, column 1"] == "< 5 min" and cells["slide 2, row 0, column 0"] == "KPI"
    s3 = [i for i in res.items if i["anchor"].get("slide") == 3]
    assert len(s3) == 1 and s3[0]["completeness"] == "incomplete" and "OCR" in s3[0]["completeness_reason"] and s3[0]["text"] == ""
    assert not res.complete and [i.location for i in res.issues] == ["sp://site/deck.pptx#slide=3"]
    assert [i["fragment_index"] for i in res.items] == list(range(len(res.items)))


def _rezip(src: bytes, mutate) -> bytes:
    with zipfile.ZipFile(BytesIO(src)) as zin:
        members = {n: zin.read(n) for n in zin.namelist()}
    mutate(members)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, d in members.items():
            zout.writestr(n, d)
    return buf.getvalue()


def test_office_parsers_reject_attacks_and_wrong_types():
    docx = (OFFICE / "review.docx").read_bytes()
    pptx = (OFFICE / "deck.pptx").read_bytes()
    for parser in (DocxParser(), PptxParser()):
        with pytest.raises(ValidationError):
            parser.parse(b"", source_ref="f")
        with pytest.raises(ValidationError):
            parser.parse(b"garbage" * 100, source_ref="f")
    with pytest.raises(ValidationError):
        DocxParser().parse(pptx, source_ref="f")
    with pytest.raises(ValidationError):
        PptxParser().parse(docx, source_ref="f")
    with pytest.raises(PolicyError, match="macro"):
        DocxParser().parse(_rezip(docx, lambda m: m.__setitem__("word/vbaProject.bin", b"\x00")), source_ref="f")
    with pytest.raises(PolicyError, match="ENTITY"):
        PptxParser().parse(
            _rezip(pptx, lambda m: m.__setitem__("ppt/slides/slide1.xml", b"<!DOCTYPE x [<!ENTITY e 'x'>]>" + m["ppt/slides/slide1.xml"])),
            source_ref="f",
        )
    with pytest.raises(PolicyError, match="traversal"):
        DocxParser().parse(_rezip(docx, lambda m: m.__setitem__("../../etc/x", b"x")), source_ref="f")
    with pytest.raises(ValidationError):
        DocxParser().parse(docx[: len(docx) // 2], source_ref="f")
