"""Knowledge domain: documents, versions, fragments, principals and source permissions.
Anchors are deterministic (page/slide/sheet/paragraph index) so citations are reproducible."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .common import DomainRecord


class Completeness(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"  # e.g. image-only slide without OCR
    UNKNOWN = "unknown"


class Anchor(BaseModel):
    """Where in the source a fragment lives. Only populated fields are meaningful."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(description="paragraph | table_cell | slide | notes | sheet_cell | page | row")
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    paragraph_index: int | None = None
    table_index: int | None = None
    row: int | None = None
    column: int | None = None
    shape_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None

    def citation(self) -> str:
        parts: list[str] = []
        if self.page is not None:
            parts.append(f"page {self.page}")
        if self.slide is not None:
            parts.append(f"slide {self.slide}")
        if self.sheet is not None:
            parts.append(f"sheet {self.sheet}")
        if self.paragraph_index is not None:
            parts.append(f"paragraph {self.paragraph_index}")
        if self.table_index is not None:
            parts.append(f"table {self.table_index}")
        if self.row is not None:
            parts.append(f"row {self.row}")
        if self.column is not None:
            parts.append(f"column {self.column}")
        return ", ".join(parts) or self.kind


class Document(DomainRecord):
    document_id: str
    title: str | None
    mime_type: str | None
    path: str | None = None
    size: int | None = None
    acl_ref: str | None = None
    deleted: bool = False


class DocumentVersion(DomainRecord):
    document_id: str
    version_id: str
    content_hash: str
    modified_at_iso: str | None = None


class Fragment(DomainRecord):
    document_id: str
    version_id: str | None
    fragment_index: int
    text: str
    anchor: Anchor
    completeness: Completeness = Completeness.COMPLETE
    completeness_reason: str | None = None


class PrincipalKind(StrEnum):
    USER = "user"
    GROUP = "group"
    LINK = "link"  # sharing link / anyone-with-link
    UNKNOWN = "unknown"


class Principal(DomainRecord):
    principal_id: str
    kind: PrincipalKind
    display_name: str | None = None
    email: str | None = None
    member_of: list[str] = Field(default_factory=list)


class SourcePermission(DomainRecord):
    document_id: str
    principal_id: str
    role: str
    inherited: bool = False
    acl_version: str
