"""Request and response shapes."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


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
    created_at: datetime


class PageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page_number: int
    render_path: str | None
    thumb_path: str | None


class SourceFileDetailOut(BaseModel):
    source_file: SourceFileOut
    pages: list[PageOut]


class PageHitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page_number: int
    # Contains <mark> tags from ts_headline. The client renders it as markup and
    # is responsible for allowing only <mark>.
    snippet: str
    rank: float


class SearchResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_file_id: uuid.UUID
    library_id: uuid.UUID
    original_filename: str | None
    page_count: int | None
    state: str
    received_at: datetime
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
    "DocumentOut",
    "FacetOut",
    "HealthOut",
    "JobOut",
    "LibraryOut",
    "LoginRequest",
    "PageHitOut",
    "PageOut",
    "PipelineStatusOut",
    "SearchResponseOut",
    "SearchResultOut",
    "SourceFileDetailOut",
    "SourceFileOut",
    "StageCount",
    "UploadResult",
    "UserOut",
]
