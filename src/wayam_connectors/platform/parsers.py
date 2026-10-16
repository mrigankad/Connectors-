"""Pick a format adapter for fetched bytes. Explicit table; unknown formats are reported, never guessed."""

from __future__ import annotations

PARSER_BY_EXTENSION: dict[str, str] = {".docx": "files.docx", ".pptx": "files.pptx"}
PARSER_BY_MIME: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "files.docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "files.pptx",
}


def parser_for(name: str, content_type: str | None) -> str | None:
    lower = name.lower()
    for ext, cid in PARSER_BY_EXTENSION.items():
        if lower.endswith(ext):
            return cid
    if content_type:
        return PARSER_BY_MIME.get(content_type.split(";")[0].strip().lower())
    return None
