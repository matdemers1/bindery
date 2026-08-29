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


class ExtractionOut(BaseModel):
    """How much text this document's pages actually carry.

    Zero is not a detail — it means nothing about this document is findable, and
    every field the model produced was guesswork over an empty page. The screen
    needs to be able to say so and offer a way out.
    """

    characters: int
    pages: int
    empty_pages: int

    @property
    def has_text(self) -> bool:
        return self.characters > 0


class WhyPanelOut(BaseModel):
    document: DocumentOut
    classification: ClassificationOut | None
    provenance: list[FieldProvenanceOut]
    tags: list[TagOut]
    extraction: ExtractionOut
    # So the screen can offer a rescan without a second round trip.
    source_file_id: uuid.UUID


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
    # A classify job carries only this, and without it a failed classification
    # on the pipeline screen is a row you cannot click through to.
    document_id: uuid.UUID | None
    stage: str
    state: str
    attempts: int
    last_error: str | None
    # A queued job with attempts > 0 is retrying after a failure, and this is
    # when it next runs — without it the screen can only say "queued", which
    # reads as "fine".
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
    # Waiting for you today.
    needs_review: int = 0
    # Waiting on the backlog surface, which is a different queue with a lower
    # bar. Kept apart so a header can never send you to an empty screen.
    backlog_pending: int = 0
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


class ModelChoiceOut(BaseModel):
    id: str
    name: str
    blurb: str
    input_per_mtok: float
    output_per_mtok: float


class SettingsOut(BaseModel):
    """What the client is allowed to know. Never the key itself."""

    # The closed set the picker offers. Sent with the settings so the UI never
    # has to carry its own copy of the model list and drift out of step.
    available_models: list[ModelChoiceOut] = []
    anthropic_key_configured: bool
    # Last four characters, so you can tell which key is loaded without seeing it.
    anthropic_key_hint: str | None
    model: str
    prompt_version: str
    # Push endpoints usually carry their credential in the URL, so this is
    # treated as a secret too: configured-or-not, never the value.
    notify_webhook_configured: bool = False
    notify_webhook_hint: str | None = None


class SettingsUpdateIn(BaseModel):
    # None means "leave alone"; empty string means "clear it".
    anthropic_api_key: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    notify_webhook_url: str | None = None


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
    "AuditEventOut",
    "AuditPageOut",
    "BackupOut",
    "BulkEditIn",
    "BulkResultOut",
    "ClassificationOut",
    "CorrespondentOut",
    "DocumentDetailOut",
    "DocumentOut",
    "DuplicatePairOut",
    "ExportOut",
    "ExportRequestIn",
    "FacetOut",
    "FieldProvenanceOut",
    "FileTreeNodeOut",
    "FileTreeOut",
    "GoBagIn",
    "HealthOut",
    "ImportItemOut",
    "ImportSessionOut",
    "ImportStartIn",
    "IntegrityOut",
    "JobOut",
    "KnownFormOut",
    "LibraryOut",
    "LoginRequest",
    "MergeIn",
    "MergePreviewOut",
    "MirrorOut",
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


# --------------------------------------------------------------------------
# Phase 6 — trust, export and resilience
# --------------------------------------------------------------------------


class ExportRequestIn(BaseModel):
    name: str = "bindery-export"


class GoBagIn(BaseModel):
    # Not stored, never logged, and never written to the settings table: the
    # passphrase exists only for the length of this request.
    passphrase: str = Field(min_length=12)


class ExportOut(BaseModel):
    path: str
    document_count: int
    file_count: int
    byte_size: int
    encrypted: bool = False
    missing_blobs: list[str] = []


class IntegrityOut(BaseModel):
    started_at: datetime
    finished_at: datetime | None
    checked: int
    bytes_read: int
    ok: int
    healthy: bool
    missing: list[dict]
    corrupt: list[dict]
    orphan_count: int
    orphans: list[str]


class MirrorOut(BaseModel):
    root: str
    linked: int
    copied: int
    missing: int
    bundles: int
    removed: int


class BackupOut(BaseModel):
    path: str
    blob_count: int
    byte_size: int
    integrity_healthy: bool
    manifest: dict


class AuditEventOut(BaseModel):
    id: uuid.UUID
    sequence: int
    entity_type: str
    entity_id: uuid.UUID
    action: str
    actor_type: str
    actor_label: str | None = None
    rule_id: uuid.UUID | None = None
    before: dict | None = None
    after: dict | None = None
    created_at: datetime


class AuditPageOut(BaseModel):
    events: list[AuditEventOut]
    next_before_sequence: int | None = None


class FileTreeNodeOut(BaseModel):
    """One row of the browsable file tree — a folder, or a document inside one."""

    path: str
    name: str
    kind: str  # "folder" | "document" | "bundle"
    document_id: uuid.UUID | None = None
    source_file_id: uuid.UUID | None = None
    title: str | None = None
    document_date: date | None = None
    correspondent: str | None = None
    document_type: str | None = None
    tags: list[str] = []
    page_start: int | None = None
    page_end: int | None = None
    page_count: int | None = None
    ingest_source: str | None = None
    received_at: datetime | None = None
    original_filename: str | None = None
    sensitivity: str | None = None
    review_state: str | None = None
    byte_size: int | None = None
    # Which library this is in. Cross-library search is impossible, so a
    # document in the wrong one is invisible rather than merely misfiled —
    # browsing has to make the assignment visible (REQ-102).
    library_id: uuid.UUID | None = None
    library_name: str | None = None
    child_count: int = 0


class FileTreeOut(BaseModel):
    root: str
    nodes: list[FileTreeNodeOut]


# --------------------------------------------------------------------------
# Phase 7 — household and libraries
# --------------------------------------------------------------------------


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str


class LibraryDetailOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    your_role: str
    members: list[MemberOut]


class LibraryCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = "personal"


class MembershipIn(BaseModel):
    email: str
    role: str


class MoveRequestIn(BaseModel):
    to_library_id: uuid.UUID


class MovePlanOut(BaseModel):
    source_file_id: uuid.UUID
    from_library_id: uuid.UUID
    to_library_id: uuid.UUID
    document_count: int
    documents: list[dict]
    cleared_correspondents: list[str]
    cleared_types: list[str]
    cleared_tags: list[str]
    loses_metadata: bool


# --------------------------------------------------------------------------
# Phase 8 — health and Q&A
# --------------------------------------------------------------------------


class AlertOut(BaseModel):
    severity: str
    code: str
    message: str
    detail: dict


class HealthPanelOut(BaseModel):
    checked_at: datetime
    healthy: bool
    queue_depth: dict[str, int]
    running: int
    failed_24h: int
    dead_letter: int
    stuck_jobs: list[dict]
    oldest_queued_seconds: float | None
    stalled: bool
    files_by_state: dict[str, int]
    spend_30d_usd: float
    spend_by_day: list[dict]
    alerts: list[AlertOut]


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class CitationOut(BaseModel):
    document_id: uuid.UUID
    source_file_id: uuid.UUID
    title: str
    page_number: int
    quote: str


class ConsultedOut(BaseModel):
    document_id: uuid.UUID
    source_file_id: uuid.UUID
    title: str
    page_number: int


class AskOut(BaseModel):
    question: str
    # None whenever there is nothing honest to say — including when the model
    # answered without citing anything (REQ-116).
    answer: str | None
    citations: list[CitationOut]
    consulted: list[ConsultedOut]
    unavailable_reason: str | None
    model: str | None


class ApiTokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1)
    # Empty means "every library the creator is in", re-intersected on each use.
    library_ids: list[uuid.UUID] = []
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiTokenOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    library_ids: list[uuid.UUID]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiTokenIssuedOut(ApiTokenOut):
    # The only time this value exists outside the caller's hands. It is not
    # stored, cannot be recovered, and never appears in an audit event.
    secret: str


class PendingReasonOut(BaseModel):
    code: str
    label: str
    detail: str
    count: int
    document_ids: list[uuid.UUID]


class PendingReviewOut(BaseModel):
    total: int
    reasons: list[PendingReasonOut]


class ReclassifyIn(BaseModel):
    document_ids: list[uuid.UUID] = []
    # "Everything waiting", optionally narrowed to particular reasons — so
    # "retry the ones that failed" and "review the ones that never ran" are
    # separate decisions rather than one blunt button.
    all_pending: bool = False
    reasons: list[str] = []


class ReclassifyResultOut(BaseModel):
    queued: int
    requested: int


class RescanResultOut(BaseModel):
    source_file_id: uuid.UUID
    queued: bool
    detail: str


class OcrPageTextOut(BaseModel):
    page_number: int
    # Verbatim, including the line breaks OCR produced: the layout is part of
    # what you are checking when you compare it against the page.
    text: str
    characters: int


class OcrTextOut(BaseModel):
    source_file_id: uuid.UUID
    original_filename: str | None
    pages: list[OcrPageTextOut]
    characters: int
    empty_pages: int


# --------------------------------------------------------------------------
# Diagnostics and per-file pipeline progress
# --------------------------------------------------------------------------


class LogEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence: int
    level: str
    logger: str
    message: str
    # Whole, not truncated: the bottom of a traceback is the useful end.
    detail: str | None
    source_file_id: uuid.UUID | None
    document_id: uuid.UUID | None
    job_id: uuid.UUID | None
    stage: str | None
    context: dict
    created_at: datetime


class LogPageOut(BaseModel):
    entries: list[LogEntryOut]
    next_before_sequence: int | None = None
    # If this is climbing, what you are reading is behind reality.
    pending_writes: int = 0


class FileProgressOut(BaseModel):
    source_file_id: uuid.UUID
    original_filename: str | None
    byte_size: int
    page_count: int | None
    state: str
    received_at: datetime
    ingest_source: str
    document_count: int
    active_stage: str | None
    failed_stage: str | None
    last_error: str | None
    attempts: int
    dead_lettered: bool


class PipelineFilesOut(BaseModel):
    files: list[FileProgressOut]
    stages: list[str]


class PhotoOut(BaseModel):
    document_id: uuid.UUID
    source_file_id: uuid.UUID
    page: int
    title: str | None
    summary: str | None
    original_filename: str | None
    received_at: datetime
    document_date: date | None
    # Whether anything has actually said what this picture is.
    described: bool


class PhotoWallOut(BaseModel):
    total: int
    photos: list[PhotoOut]


class UnifyMemberOut(BaseModel):
    id: uuid.UUID
    name: str
    documents: int


class UnifyGroupOut(BaseModel):
    canonical: str
    canonical_id: uuid.UUID | None
    reason: str
    document_count: int
    members: list[UnifyMemberOut]


class UnifyProposalOut(BaseModel):
    considered: int
    model: str | None
    unavailable_reason: str | None
    groups: list[UnifyGroupOut]


class UnifyApplyIn(BaseModel):
    canonical_id: uuid.UUID
    # Named explicitly so what gets applied is what was shown on screen, not
    # whatever the model would say if it were asked a second time.
    member_ids: list[uuid.UUID]
