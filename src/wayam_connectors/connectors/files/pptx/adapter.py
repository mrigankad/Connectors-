"""PowerPoint PPTX parser: slide text, tables and notes with slide-number citations.

Image-only slides (pictures but no text) are emitted as INCOMPLETE fragments with a
reason, so a retrieval product can show "slide 7 has content we cannot read" instead
of silently skipping it. OCR is not implemented.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from io import BytesIO
from typing import Any

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from wayam_connectors.contracts.capabilities import ParseIssue, ParseResult
from wayam_connectors.contracts.domains.knowledge import Anchor, Completeness
from wayam_connectors.contracts.errors import ValidationError
from wayam_connectors.platform.security.files import DEFAULT_LIMITS, FileLimits, inspect_ooxml_zip

ADAPTER_VERSION = "0.1.0"
CONNECTOR_ID = "files.pptx"


class PptxParser:
    CONNECTOR_ID = CONNECTOR_ID
    ADAPTER_VERSION = ADAPTER_VERSION

    def __init__(self, limits: FileLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def parse(self, data: bytes, *, source_ref: str, options: Mapping[str, Any] | None = None) -> ParseResult:
        zf = inspect_ooxml_zip(data, self.limits, allowed_prefixes=("ppt/",))
        if "ppt/presentation.xml" not in zf.namelist():
            raise ValidationError("container has no ppt/presentation.xml")
        try:
            prs = Presentation(BytesIO(data))
        except Exception as exc:
            raise ValidationError("python-pptx could not open the presentation", details={"error": str(exc)[:200]}) from exc
        sha = hashlib.sha256(data).hexdigest()
        items: list[dict[str, Any]] = []
        issues: list[ParseIssue] = []
        complete = True
        total_chars = 0
        for s_i, slide in enumerate(prs.slides, start=1):
            slide_text_found = False
            has_picture = False
            pending: list[tuple[str, Anchor]] = []
            for shape in _walk(slide.shapes):
                sid = str(shape.shape_id)
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE or getattr(shape, "image", None) is not None and not shape.has_text_frame:
                    has_picture = True
                if getattr(shape, "has_table", False) and shape.has_table:
                    for r_i, row in enumerate(shape.table.rows):
                        for c_i, cell in enumerate(row.cells):
                            if cell.is_spanned:
                                continue
                            text = cell.text
                            if text.strip():
                                slide_text_found = True
                                pending.append((text, Anchor(kind="table_cell", slide=s_i, shape_id=sid, row=r_i, column=c_i)))
                    continue
                if shape.has_text_frame:
                    text = shape.text_frame.text
                    if text.strip():
                        slide_text_found = True
                        pending.append((text, Anchor(kind="slide", slide=s_i, shape_id=sid, char_start=0, char_end=len(text))))
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text if slide.notes_slide.notes_text_frame is not None else ""
                if notes.strip():
                    pending.append((notes, Anchor(kind="notes", slide=s_i)))
            if not slide_text_found and has_picture:
                complete = False
                reason = "image-only slide; OCR not implemented"
                issues.append(ParseIssue(location=f"{source_ref}#slide={s_i}", code="incomplete", message=reason))
                items.append(_fragment(len(items), "", Anchor(kind="slide", slide=s_i), Completeness.INCOMPLETE, reason))
            for text, anchor in pending:
                total_chars += len(text)
                if total_chars > self.limits.max_text_chars:
                    raise ValidationError("presentation text exceeds the configured limit")
                items.append(_fragment(len(items), text, anchor, Completeness.COMPLETE, None))
        return ParseResult(
            parser=CONNECTOR_ID,
            parser_version=ADAPTER_VERSION,
            source_sha256=sha,
            items=items,
            issues=issues,
            complete=complete,
            stats={"slides": len(prs.slides), "fragments": len(items), "chars": total_chars},
        )


def _walk(shapes: Any) -> Any:
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk(shape.shapes)
        else:
            yield shape


def _fragment(index: int, text: str, anchor: Anchor, completeness: Completeness, reason: str | None) -> dict[str, Any]:
    return {
        "fragment_index": index,
        "text": text,
        "anchor": anchor.model_dump(exclude_none=True),
        "citation": anchor.citation(),
        "completeness": completeness.value,
        "completeness_reason": reason,
    }


ADAPTER = PptxParser
