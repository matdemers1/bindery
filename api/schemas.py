"""Request and response shapes."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str | None


class LibraryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str


class SourceFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    library_id: uuid.UUID
    sha256: str
    byte_size: int
    mime_type: str | None
    original_filename: str | None
    ingest_source: str
    page_count: int | None
    state: str
    received_at: datetime


class UploadResult(BaseModel):
    source_file: SourceFileOut
    # True when these exact bytes were already in the archive, so nothing new
    # was stored. The caller is told rather than silently given a second row.
    duplicate: bool


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    library_id: uuid.UUID
    source_file_id: uuid.UUID
    page_start: int
    page_end: int
    title: str | None
    summary: str | None
    document_date: date | None
    sensitivity: str
    redundancy: str
    review_state: str
    is_backlog: bool
    known_form_id: uuid.UUID | None
    created_at: datetime

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1


class SegmentIn(BaseModel):
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    title: str | None = None


class SegmentReplaceIn(BaseModel):
    """The complete segment set for a file.

    A partial edit has no valid intermediate state — segments must always cover
    the file exactly — so the whole cover is submitted at once.
    """

    segments: list[SegmentIn] = Field(min_length=1)


class SegmentListOut(BaseModel):
    source_file_id: uuid.UUID
    page_count: int
    segments: list[DocumentOut]


class KnownFormOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None
    enabled: bool


class PageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page_number: int
    render_path: str | None
    thumb_path: str | None


class SourceFileDetailOut(BaseModel):
    source_file: SourceFileOut
    pages: list[PageOut]


class DocumentDetailOut(BaseModel):
    """Everything the viewer needs to show a page range as a standalone document."""

    document: DocumentOut
    source_file: SourceFileOut
    known_form: KnownFormOut | None
    pages: list[PageOut]


class PageHitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # Absolute position in the source file.
    page_number: int
    # Position within this document. Page numbers are always disambiguated
    # (REQ-030): "page 2 of this document (page 48 of the file)".
    document_page_number: int
    # Contains <mark> tags from ts_headline. The client renders it as markup and
    # is responsible for allowing only <mark>.
    snippet: str
    rank: float


class SearchResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    source_file_id: uuid.UUID
    library_id: uuid.UUID
    title: str | None
    original_filename: str | None
    page_start: int
    page_end: int
    file_page_count: int | None
    state: str
    received_at: datetime
    known_form_code: str | None
    known_form_name: str | None
    best_page: PageHitOut
    matching_pages: int


class FacetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    value: str
    label: str
    count: int


class SearchResponseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    query: str
    total: int
    results: list[SearchResultOut]
    facets: dict[str, list[FacetOut]]
    suggestions: list[str]


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_file_id: uuid.UUID | None
    stage: str
    state: str
    attempts: int
    last_error: str | None
    scheduled_for: datetime
    updated_at: datetime


class StageCount(BaseModel):
    stage: str
    state: str
    count: int


class PipelineStatusOut(BaseModel):
    counts: list[StageCount]
    # Jobs a human is expected to look at — failed and dead-lettered.
    attention: list[JobOut]
    in_flight: list[JobOut]


class HealthOut(BaseModel):
    status: str
    database: str
    version: str


__all__ = [
    "DocumentDetailOut",
    "DocumentOut",
    "FacetOut",
    "HealthOut",
    "JobOut",
    "KnownFormOut",
    "LibraryOut",
    "LoginRequest",
    "PageHitOut",
    "PageOut",
    "PipelineStatusOut",
    "SearchResponseOut",
    "SearchResultOut",
    "SegmentIn",
    "SegmentListOut",
    "SegmentReplaceIn",
    "SourceFileDetailOut",
    "SourceFileOut",
    "StageCount",
    "UploadResult",
    "UserOut",
]
