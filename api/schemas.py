"""Request and response shapes."""

import uuid
from datetime import date, datetime
from typing import Any

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


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    # ai / rule / human — what makes an AI value visually distinct (REQ-064).
    source: str


class ClassificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: uuid.UUID
    model: str
    prompt_version: str
    # Displayed, never decisive (REQ-065).
    confidence: dict[str, float]
    # The facts the gate actually read (REQ-057).
    structural_signals: dict[str, Any]
    gate_decision: str | None
    gate_reasons: list[str]
    # Typed values from a known form's extractors (REQ-041).
    extracted_fields: dict[str, str]
    usage: dict[str, Any]
    created_at: datetime


class FieldProvenanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    field_name: str
    # Absolute page in the source file, so the panel can link straight to it.
    page_number: int | None
    snippet: str | None
    confidence: float | None


class WhyPanelOut(BaseModel):
    document: DocumentOut
    classification: ClassificationOut | None
    provenance: list[FieldProvenanceOut]
    tags: list[TagOut]


class ReviewQueueOut(BaseModel):
    total: int
    documents: list[DocumentOut]


class RuleIn(BaseModel):
    library_id: uuid.UUID
    name: str = Field(min_length=1)
    priority: int = 100
    conditions: dict[str, Any]
    actions: dict[str, Any]


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    library_id: uuid.UUID
    name: str
    enabled: bool
    priority: int
    conditions: dict[str, Any]
    actions: dict[str, Any]
    created_at: datetime


class RuleMatchOut(BaseModel):
    document_id: uuid.UUID
    title: str | None
    changes: dict[str, Any]


class RuleDryRunOut(BaseModel):
    rule_id: uuid.UUID
    examined: int
    matched: int
    truncated: bool
    matches: list[RuleMatchOut]


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


class ArchiveEntryOut(BaseModel):
    """One row of the archive browser: what it is, and how it got here."""

    document_id: uuid.UUID
    source_file_id: uuid.UUID
    title: str | None
    original_filename: str | None
    page_start: int
    page_end: int
    file_page_count: int | None
    document_date: date | None
    received_at: datetime
    # web_upload | watched_folder | camera | bulk_import
    ingest_source: str
    correspondent: str | None
    document_type: str | None
    known_form: str | None
    review_state: str
    sensitivity: str
    is_backlog: bool
    sha256: str
    tags: list["TagOut"]


class ArchiveStatsOut(BaseModel):
    documents: int = 0
    files: int = 0
    pages: int = 0
    needs_review: int = 0
    unclassified: int = 0


class ArchiveOut(BaseModel):
    total: int
    entries: list[ArchiveEntryOut]
    stats: ArchiveStatsOut


class TreeGroupOut(BaseModel):
    label: str
    count: int


class TreeOut(BaseModel):
    group_by: str
    groups: list[TreeGroupOut]


class ImportStartIn(BaseModel):
    library_id: uuid.UUID
    # Absolute, and resolved inside the worker container — not on your laptop.
    root_path: str
    sample_size: int = Field(200, ge=1, le=5000)


class ImportSessionOut(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    root_path: str
    state: str
    pass_number: int
    sample_size: int
    dry_run: dict[str, Any]
    cost_estimate: dict[str, Any]
    progress: dict[str, int]
    last_error: str | None
    created_at: datetime


class ImportItemOut(BaseModel):
    path: str
    state: str
    byte_size: int | None
    sha256: str | None
    source_file_id: uuid.UUID | None
    error: str | None


class BulkEditIn(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1)
    actions: dict[str, Any]


class BulkResultOut(BaseModel):
    matched: int
    # Present only after an apply; this is what undo takes.
    operation_id: uuid.UUID | None
    changes: list[dict[str, Any]]


class CorrespondentOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str | None
    # Every spelling that resolves here. Aliases are what keep merge rare.
    aliases: list[str]
    document_count: int


class MergeIn(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID


class MergePreviewOut(BaseModel):
    from_name: str
    into_name: str
    document_count: int
    alias_count: int
    documents: list[dict[str, Any]]
    # Present only after a commit; this is what undo takes.
    operation_id: uuid.UUID | None = None


class AssetIn(BaseModel):
    library_id: uuid.UUID
    kind: str
    name: str = Field(min_length=1)
    # VIN, plate, address, policy number.
    attributes: dict[str, Any] = Field(default_factory=dict)


class AssetOut(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    kind: str
    name: str
    attributes: dict[str, Any]
    document_count: int


class AssetTimelineOut(BaseModel):
    asset: AssetOut
    entries: list[dict[str, Any]]


class SavedSearchIn(BaseModel):
    library_id: uuid.UUID
    name: str = Field(min_length=1)
    query: dict[str, Any] = Field(default_factory=dict)
    is_packet: bool = False


class SavedSearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    library_id: uuid.UUID
    name: str
    query: dict[str, Any]
    is_shelf: bool
    is_packet: bool
    created_at: datetime


class TaxonomyHealthOut(BaseModel):
    total_tags: int
    used_once: int
    unused: int
    orphan_ratio: float
    # R-08's tripwire: >15% of tags used exactly once means the existing_ids
    # contract is not holding.
    exceeds_alarm: bool
    near_duplicate_tags: list[dict[str, Any]]
    near_duplicate_correspondents: list[dict[str, Any]]


class SimilarOut(BaseModel):
    results: list[dict[str, Any]]


class DuplicatePairOut(BaseModel):
    id: uuid.UUID
    document_a_id: uuid.UUID
    document_b_id: uuid.UUID
    a_title: str | None
    b_title: str | None
    similarity: float


class SettingsOut(BaseModel):
    """What the client is allowed to know. Never the key itself."""

    anthropic_key_configured: bool
    # Last four characters, so you can tell which key is loaded without seeing it.
    anthropic_key_hint: str | None
    model: str
    prompt_version: str


class SettingsUpdateIn(BaseModel):
    # None means "leave alone"; empty string means "clear it".
    anthropic_api_key: str | None = None
    model: str | None = None
    prompt_version: str | None = None


class SettingsTestOut(BaseModel):
    ok: bool
    detail: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class HealthOut(BaseModel):
    status: str
    database: str
    version: str


__all__ = [
    "ArchiveEntryOut",
    "ArchiveOut",
    "ArchiveStatsOut",
    "AssetIn",
    "AssetOut",
    "AssetTimelineOut",
    "BulkEditIn",
    "BulkResultOut",
    "ClassificationOut",
    "CorrespondentOut",
    "DocumentDetailOut",
    "DocumentOut",
    "DuplicatePairOut",
    "FacetOut",
    "FieldProvenanceOut",
    "HealthOut",
    "ImportItemOut",
    "ImportSessionOut",
    "ImportStartIn",
    "JobOut",
    "KnownFormOut",
    "LibraryOut",
    "LoginRequest",
    "MergeIn",
    "MergePreviewOut",
    "PageHitOut",
    "PageOut",
    "PipelineStatusOut",
    "ReviewQueueOut",
    "RuleDryRunOut",
    "RuleIn",
    "RuleMatchOut",
    "RuleOut",
    "SavedSearchIn",
    "SavedSearchOut",
    "SearchResponseOut",
    "SearchResultOut",
    "SegmentIn",
    "SegmentListOut",
    "SegmentReplaceIn",
    "SettingsOut",
    "SettingsTestOut",
    "SettingsUpdateIn",
    "SimilarOut",
    "SourceFileDetailOut",
    "SourceFileOut",
    "StageCount",
    "TagOut",
    "TaxonomyHealthOut",
    "TreeGroupOut",
    "TreeOut",
    "UploadResult",
    "UserOut",
    "WhyPanelOut",
]
