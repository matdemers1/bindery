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


class HealthOut(BaseModel):
    status: str
    database: str
    version: str


__all__ = [
    "DocumentOut",
    "HealthOut",
    "LibraryOut",
    "LoginRequest",
    "SourceFileOut",
    "UploadResult",
    "UserOut",
]
