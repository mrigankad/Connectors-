"""Word DOCX parser: body paragraphs and tables with deterministic anchors.

Bytes come from an already authorized source (SharePoint, OneDrive, S3, upload). The
container is inspected for traversal, bombs, macros and XML entity declarations
before python-docx opens it. Fragments are emitted in document order, each with an
anchor a product can cite: paragraph index (0-based over body paragraphs) or
table/row/column. Headers, footers, footnotes, comments, text boxes and images are
not extracted (listed as limitations) and the result is marked incomplete when
images or text boxes are present so retrieval does not claim full coverage.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from io import BytesIO
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from wayam_connectors.contracts.capabilities import ParseIssue, ParseResult
from wayam_connectors.contracts.domains.knowledge import Anchor, Completeness
from wayam_connectors.contracts.errors import ValidationError
from wayam_connectors.platform.security.files import DEFAULT_LIMITS, FileLimits, inspect_ooxml_zip

ADAPTER_VERSION = "0.1.0"
CONNECTOR_ID = "files.docx"


class DocxParser:
    CONNECTOR_ID = CONNECTOR_ID
    ADAPTER_VERSION = ADAPTER_VERSION

    def __init__(self, limits: FileLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def parse(self, data: bytes, *, source_ref: str, options: Mapping[str, Any] | None = None) -> ParseResult:
        zf = inspect_ooxml_zip(data, self.limits, allowed_prefixes=("word/",))
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise ValidationError("container has no word/document.xml")
        try:
            doc = Document(BytesIO(data))
        except Exception as exc:  # python-docx raises a range of exceptions for damaged packages
            raise ValidationError("python-docx could not open the document", details={"error": str(exc)[:200]}) from exc
        sha = hashlib.sha256(data).hexdigest()
        items: list[dict[str, Any]] = []
        issues: list[ParseIssue] = []
        para_index = table_index = 0
        total_chars = 0
        body = doc.element.body
        for child in body.iterchildren():
            tag = child.tag
            if tag == qn("w:p"):
                p = Paragraph(child, doc)
                text = p.text
                if text.strip():
                    total_chars += len(text)
                    items.append(
                        _fragment(
                            len(items),
                            text,
                            Anchor(kind="paragraph", paragraph_index=para_index, char_start=0, char_end=len(text)),
                            style=p.style.name if p.style is not None else None,
                        )
                    )
                para_index += 1
            elif tag == qn("w:tbl"):
                t = Table(child, doc)
                for r_i, row in enumerate(t.rows):
                    seen: set[int] = set()
                    for c_i, cell in enumerate(row.cells):
                        if id(cell._tc) in seen:  # merged cells repeat the same tc
                            continue
                        seen.add(id(cell._tc))
                        text = cell.text
                        if text.strip():
                            total_chars += len(text)
                            items.append(
                                _fragment(len(items), text, Anchor(kind="table_cell", table_index=table_index, row=r_i, column=c_i))
                            )
                table_index += 1
            if total_chars > self.limits.max_text_chars:
                raise ValidationError("document text exceeds the configured limit")
        has_images = any(n.startswith("word/media/") for n in names)
        has_textboxes = bool(body.findall(".//" + qn("w:txbxContent")))
        has_footnotes = "word/footnotes.xml" in names and bool(doc.part.package)
        complete = not (has_images or has_textboxes)
        reason = None
        if not complete:
            parts = []
            if has_images:
                parts.append("images are not extracted (no OCR)")
            if has_textboxes:
                parts.append("text boxes are not extracted")
            reason = "; ".join(parts)
            issues.append(ParseIssue(location=source_ref, code="incomplete", message=reason))
            for it in items:
                it["completeness"] = Completeness.INCOMPLETE.value
                it["completeness_reason"] = reason
        if has_footnotes:
            issues.append(ParseIssue(location=source_ref, code="not_extracted", message="footnotes present but not extracted"))
        return ParseResult(
            parser=CONNECTOR_ID,
            parser_version=ADAPTER_VERSION,
            source_sha256=sha,
            items=items,
            issues=issues,
            complete=complete,
            stats={"paragraphs": para_index, "tables": table_index, "fragments": len(items), "chars": total_chars, "images": has_images},
        )


def _fragment(index: int, text: str, anchor: Anchor, style: str | None = None) -> dict[str, Any]:
    out = {
        "fragment_index": index,
        "text": text,
        "anchor": anchor.model_dump(exclude_none=True),
        "citation": anchor.citation(),
        "completeness": Completeness.COMPLETE.value,
        "completeness_reason": None,
    }
    if style:
        out["style"] = style
    return out


ADAPTER = DocxParser
