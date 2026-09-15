"""Request and response shapes.

Grouped by **domain**, in the same seams the rest of the codebase already uses —
the 21 routers, `web/src/features/<domain>/`, `api/vault/`, `api/export/`,
`api/search/`. The banners used to name build phases instead (CR-090), which is
an index only somebody who lived through the phases can read, and it had already
stopped being maintained: the vault, editing and photo models had all landed
outside the region their phase number implied.

Adding a model: put it under the domain banner it belongs to. If it belongs to
none of them, add a banner — a new phase number is not a place.
"""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Authentication and the caller's own identity
# --------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str
    # A TOTP code or a recovery code, when the account has two-factor enrolled.
    # Optional on the model so the first request can be answered with "and now
    # the code", rather than the form having to know in advance — which it
    # cannot, because knowing would tell an anonymous caller whether the account
    # exists and whether it is protected.
    code: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str | None
    # So the shell knows whether to draw the People link. Hiding it is
    # presentation only — `require_admin` answers 404 to everyone else.
    is_admin: bool = False


# --------------------------------------------------------------------------
# Libraries, source files, documents and segments
# --------------------------------------------------------------------------


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
    # Needed by the edit form to show what is currently set (REQ-188). Ids
    # rather than names, because that is what an edit sends back — a form that
    # displayed a name and posted a name would be resolving taxonomy by string,
    # which invariant 6 forbids.
    correspondent_id: uuid.UUID | None = None
    document_type_id: uuid.UUID | None = None
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


# --------------------------------------------------------------------------
# Corrections — editing a document by hand, and who owns each field
# --------------------------------------------------------------------------


class DocumentEditIn(BaseModel):
    """A correction (REQ-188).

    Every field is optional, and **absent is not the same as null**: a payload
    that does not mention `title` leaves it alone, while one that sends
    `"title": null` clears it. The route reads `model_fields_set` to tell them
    apart, which is the only way "remove the wrong date" can be expressed.

    Taxonomy is by id (invariant 6). Creating a new correspondent, type or tag
    is a separate, explicit field — a name never becomes a link by resembling
    something that already exists.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    summary: str | None = None
    document_date: date | None = None
    correspondent_id: uuid.UUID | None = None
    document_type_id: uuid.UUID | None = None

    # Explicit creation, not a fallback for an unmatched name.
    create_correspondent: str | None = None
    create_document_type: str | None = None

    add_tag_ids: list[uuid.UUID] = []
    remove_tag_ids: list[uuid.UUID] = []
    create_tags: list[str] = []


class DocumentEditOut(BaseModel):
    """What actually changed, so the UI can say so rather than assume."""

    document: "DocumentOut"
    changed: list[str] = []
    tags_added: list[str] = []
    tags_removed: list[str] = []
    created: dict[str, str] = {}
    event_id: uuid.UUID | None = None


class FieldSourceOut(BaseModel):
    """Who set one field, for the visual distinction REQ-064 requires."""

    field_name: str
    source: str
    set_by: uuid.UUID | None = None
    set_at: datetime | None = None
    event_id: uuid.UUID | None = None


# --------------------------------------------------------------------------
# Media metadata, and the full document view
# --------------------------------------------------------------------------


class MediaMetadataOut(BaseModel):
    """What the file said about itself (REQ-193, REQ-194)."""

    model_config = ConfigDict(from_attributes=True)

    kind: str
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    captured_at: datetime | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    codec: str | None = None
    frame_rate: float | None = None
    browser_playable: bool = True


class DocumentDetailOut(BaseModel):
    """Everything the viewer needs to show a page range as a standalone document."""

    document: DocumentOut
    source_file: SourceFileOut
    known_form: KnownFormOut | None
    pages: list[PageOut]
    # Who set each field. Empty for a document nobody has corrected and no
    # classification has claimed — which is every document older than Phase 17.
    field_sources: list[FieldSourceOut] = []
    # Live tag links, with the source on each. The edit form needs to know what
    # is on the document before it can offer to take any of it off.
    # Forward reference: TagOut is defined below, and moving it up here
    # would separate it from the other taxonomy shapes for no gain.
    tags: list["TagOut"] = []
    # Present for photographs and videos; None for a scan of a form.
    media: MediaMetadataOut | None = None


# --------------------------------------------------------------------------
# Search — page hits, facets and results
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Taxonomy, classification, provenance and the review queue
# --------------------------------------------------------------------------


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    # ai / rule / human — what makes an AI value visually distinct (REQ-064).
    source: str


class TaxonomyOptionOut(BaseModel):
    """One pickable tag or document type, for the edit form (REQ-190).

    Just an id, a name and a count — the count is what makes an accidental
    near-duplicate obvious at the moment of choosing rather than on the
    Organise screen a month later.
    """

    id: uuid.UUID
    name: str
    document_count: int = 0


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
    # Who decided each field (REQ-064). `provenance` above explains AI values
    # and can only ever explain AI values — it hangs off a classification. This
    # is the half that can say "a person set this", which is what makes the
    # panel answer the question people actually ask of it.
    field_sources: list["FieldSourceOut"] = []


class ReviewQueueOut(BaseModel):
    total: int
    documents: list[DocumentOut]


# --------------------------------------------------------------------------
# Rules — the deterministic override
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Jobs and pipeline status
# --------------------------------------------------------------------------


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
    # Set when someone has seen a dead letter and accepted it. Never means
    # deleted, retried or hidden — only "stop counting this as outstanding".
    acknowledged_at: datetime | None = None


class StageCount(BaseModel):
    stage: str
    state: str
    count: int


class PipelineStatusOut(BaseModel):
    counts: list[StageCount]
    # Jobs a human is expected to look at — failed and dead-lettered.
    attention: list[JobOut]
    in_flight: list[JobOut]
    # Correctly refused inputs. Listed, never alarmed on.
    declined: list[JobOut] = []


# --------------------------------------------------------------------------
# Browsing the archive
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Backlog import
# --------------------------------------------------------------------------


class ImportStartIn(BaseModel):
    # Seal every document this import produces, as its pipeline finishes,
    # while the creator's vault is open. Refused (423) if the vault is shut
    # at creation, so the intent is real rather than a box nobody can honour.
    to_vault: bool = False
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
    # Bound for the vault (REQ-197). `vaulted` counts documents already sealed;
    # `awaiting_vault` counts the ones whose pipeline has finished but that are
    # waiting for the vault to be open. The difference between "done" and
    # "unlock to continue" has to be visible, or the second looks like a bug.
    to_vault: bool = False
    vaulted: int = 0
    awaiting_vault: int = 0
    vault_unlocked: bool = False


class ImportPresetsOut(BaseModel):
    """Paths the screen can offer without anyone typing them."""

    inbox: str


class ImportLogLineOut(BaseModel):
    at: datetime
    level: str
    message: str
    source_file_id: uuid.UUID | None = None
    stage: str | None = None


class ImportItemOut(BaseModel):
    path: str
    state: str
    byte_size: int | None
    sha256: str | None
    source_file_id: uuid.UUID | None
    error: str | None


# --------------------------------------------------------------------------
# Bulk edit
# --------------------------------------------------------------------------


class BulkEditIn(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1)
    actions: dict[str, Any]


class BulkResultOut(BaseModel):
    matched: int
    # Present only after an apply; this is what undo takes.
    operation_id: uuid.UUID | None
    changes: list[dict[str, Any]]


# --------------------------------------------------------------------------
# Entities — correspondents, assets, shelves, taxonomy health, duplicates
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Settings, and the closed set of models
# --------------------------------------------------------------------------


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

    # Offsite replication (REQ-159, ADR-010). The secret access key follows the
    # same rule as everything else here — configured-or-not and four characters.
    # The access key id, the bucket, the region and the KMS key id are returned
    # in full: none is a credential, and every one of them is something you need
    # to be able to read back during a restore or a rotation.
    aws_access_key_id: str | None = None
    aws_secret_configured: bool = False
    aws_secret_hint: str | None = None
    offsite_bucket: str | None = None
    offsite_region: str | None = None
    offsite_kms_key_id: str | None = None


class SettingsUpdateIn(BaseModel):
    # None means "leave alone"; empty string means "clear it".
    anthropic_api_key: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    notify_webhook_url: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    offsite_bucket: str | None = None
    offsite_region: str | None = None
    offsite_kms_key_id: str | None = None


class SettingsTestOut(BaseModel):
    ok: bool
    detail: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


# --------------------------------------------------------------------------
# The private vault
# --------------------------------------------------------------------------


class VaultStateOut(BaseModel):
    """What a locked vault is willing to say about itself.

    Whether it exists and whether it is open, because the UI must render
    something. Not how much is in it: a locked vault that reports a count has
    already said something about its contents.
    """

    exists: bool
    unlocked: bool
    pin_enabled: bool = False
    pin_failures: int = 0


class VaultItemOut(BaseModel):
    document_id: uuid.UUID
    title: str | None = None
    original_filename: str | None = None
    byte_size: int = 0
    page_count: int = 0
    vaulted_at: datetime | None = None
    warnings: list[str] = []
    # So the vault can show photographs as photographs. Derived from the
    # filename when the stored type is null, which is every item sealed before
    # the `mime_type` typo was fixed.
    media_type: str | None = None
    is_image: bool = False
    is_video: bool = False
    # What the file said about itself, decrypted with the rest of the item.
    media: MediaMetadataOut | None = None


class VaultSearchHitOut(BaseModel):
    document_id: uuid.UUID
    title: str | None = None
    page_number: int
    snippet: str


class VaultSearchOut(BaseModel):
    query: str
    total: int
    hits: list[VaultSearchHitOut] = []
    # Reported rather than hidden: this search is linear in the size of the
    # vault, and a search that quietly got slower every month is how people
    # conclude the archive is broken (ADR-012).
    pages_scanned: int = 0
    elapsed_ms: int = 0
    slow: bool = False


# --------------------------------------------------------------------------
# Offsite replication
# --------------------------------------------------------------------------


class OffsiteRunOut(BaseModel):
    """One replication attempt, successful or not (REQ-164)."""

    id: uuid.UUID
    kind: str
    state: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None = None
    detail: str | None = None
    dump_key: str | None = None
    dump_bytes: int = 0
    blobs_uploaded: int = 0
    blobs_skipped: int = 0
    bytes_sent: int = 0
    failures: list[str] | None = None

    model_config = ConfigDict(from_attributes=True)


class OffsiteStatusOut(BaseModel):
    """What the Trust screen needs to say whether the archive has left the building.

    The age rather than a tick: "last succeeded 3 days ago" is a fact someone
    can act on, and a green tick is a claim that stops being checked.
    """

    configured: bool
    # None when nothing has ever succeeded — which reads as stale, not as new.
    last_success_at: datetime | None = None
    last_success_age_seconds: int | None = None
    stale: bool = True
    in_flight: str | None = None
    last_daily_at: datetime | None = None
    last_weekly_at: datetime | None = None
    runs_total: int = 0
    failures_since_success: int = 0
    runs: list[OffsiteRunOut] = []


class OffsiteTestOut(BaseModel):
    """The result of a real round trip, not a reachability check (REQ-160)."""

    ok: bool
    detail: str
    encryption: str | None = None
    kms_key_arn: str | None = None
    bucket_key_enabled: bool | None = None
    # What was actually proven, in order, so a partial failure says how far it
    # got rather than only that it stopped.
    checks: list[str] = []


class HealthOut(BaseModel):
    status: str
    database: str
    version: str



# --------------------------------------------------------------------------
# Trust — export, integrity, mirror, backup, the audit log and the file tree
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
    # Originals that are encrypted rather than absent. Shown so the count of
    # checked originals adds up on screen without looking like loss.
    sealed: list[dict] = []
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
# Household — members, library detail, and moving a file between libraries
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
# Health, Ask, API tokens, re-classification and raw OCR text
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


class FileMatchesOut(BaseModel):
    """Which pages of one file match a query (D-03).

    Page numbers only. The viewer already knows how to render a page and how to
    label it; what it lacked was the set to step through.
    """

    query: str
    pages: list[int]


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


# --------------------------------------------------------------------------
# Photographs, and the unify proposal
# --------------------------------------------------------------------------


class PhotoOut(BaseModel):
    document_id: uuid.UUID
    source_file_id: uuid.UUID
    page: int
    title: str | None
    summary: str | None
    original_filename: str | None
    received_at: datetime
    document_date: date | None
    # How much text OCR got off the page. Zero is the common case for a
    # photograph and is the whole reason this view and the vision pass exist.
    text_chars: int
    # Whether anything has actually *looked* at this picture. A title written
    # from an empty page — "Unreadable Scan" — does not count.
    described: bool
    # image | video (Phase 18). Videos have a poster rather than a page render,
    # a duration rather than text, and a player rather than a viewer.
    kind: str = "image"
    media: MediaMetadataOut | None = None


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
    kind: str = "correspondent"
    considered: int
    model: str | None
    unavailable_reason: str | None
    groups: list[UnifyGroupOut]


class UnifyApplyIn(BaseModel):
    canonical_id: uuid.UUID
    # Named explicitly so what gets applied is what was shown on screen, not
    # whatever the model would say if it were asked a second time.
    member_ids: list[uuid.UUID]


# --------------------------------------------------------------------------
# Accounts — quota, password, two-factor, invitations and administration
# --------------------------------------------------------------------------


class QuotaOut(BaseModel):
    used_bytes: int
    quota_bytes: int | None
    files: int


class AccountOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    is_admin: bool
    totp_enabled: bool
    storage: QuotaOut


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class TotpStatusOut(BaseModel):
    enabled: bool
    # True for administrators, who cannot turn it off (REQ-156).
    required: bool


class TotpEnrolOut(BaseModel):
    secret: str
    uri: str
    # The same URI as a QR code, drawn server-side (Phase 19). Contains the
    # secret, so it travels only in this response, like `secret` does.
    qr_svg: str


class TotpConfirmIn(BaseModel):
    code: str


class InviteIn(BaseModel):
    email: str
    library_name: str = "Documents"
    storage_quota_bytes: int | None = None
    note: str | None = None


class InviteOut(BaseModel):
    """What an invitation link is for, shown before the account exists."""

    email: str
    library_name: str
    expires_at: datetime
    note: str | None
    storage_quota_bytes: int | None


class AcceptInviteIn(BaseModel):
    password: str
    display_name: str | None = None


class ResetCodeOut(BaseModel):
    """Shown once. The administrator reads it out; nothing stores it."""

    code: str
    email: str
    expires_in_hours: int


class RedeemResetIn(BaseModel):
    email: str
    code: str
    new_password: str


class SetupStateOut(BaseModel):
    """Whether this archive has been claimed — and nothing else (Phase 19).

    Unauthenticated. `unclaimed`, `needs_second_factor` or `complete`. Anyone
    who can see the setup screen learns the first fact anyway; no count, no
    address and no name is ever added here.
    """

    state: Literal["unclaimed", "needs_second_factor", "complete"]


class SetupClaimIn(BaseModel):
    """The printed setup code plus the first account. Code formatting is free:
    any case, with or without dashes and spaces."""

    code: str
    email: str
    password: str
    display_name: str | None = None
    library_name: str


class AdminAccountOut(BaseModel):
    """One account, as an administrator sees it.

    Counts, states and timestamps. No title, no filename, no page — an
    administrator administers accounts, not documents (ADR-009, REQ-143).
    """

    id: uuid.UUID
    email: str
    display_name: str | None
    is_admin: bool
    is_active: bool
    suspended_at: datetime | None
    locked_until: datetime | None
    totp_enabled: bool
    storage_quota_bytes: int | None
    used_bytes: int
    created_at: datetime

# Every model in this module, in one place, asserted by
# tests/test_schemas_index.py so it cannot fall behind again.
# It used to sit two-thirds of the way up the file and list 78 of 124 —
# every model defined after it was silently absent, which is the same
# unmaintained-index failure the domain banners above were written to fix
# (CR-090). At the bottom, it cannot be outgrown by the next class.
__all__ = [
    "AcceptInviteIn",
    "AccountOut",
    "AdminAccountOut",
    "AlertOut",
    "ApiTokenIn",
    "ApiTokenIssuedOut",
    "ApiTokenOut",
    "ArchiveEntryOut",
    "ArchiveOut",
    "ArchiveStatsOut",
    "AskIn",
    "AskOut",
    "AssetIn",
    "AssetOut",
    "AssetTimelineOut",
    "AuditEventOut",
    "AuditPageOut",
    "BackupOut",
    "BulkEditIn",
    "BulkResultOut",
    "ChangePasswordIn",
    "CitationOut",
    "ClassificationOut",
    "ConsultedOut",
    "CorrespondentOut",
    "DocumentDetailOut",
    "DocumentEditIn",
    "DocumentEditOut",
    "DocumentOut",
    "DuplicatePairOut",
    "ExportOut",
    "ExportRequestIn",
    "ExtractionOut",
    "FacetOut",
    "FieldProvenanceOut",
    "FieldSourceOut",
    "FileMatchesOut",
    "FileProgressOut",
    "FileTreeNodeOut",
    "FileTreeOut",
    "GoBagIn",
    "HealthOut",
    "HealthPanelOut",
    "ImportItemOut",
    "ImportLogLineOut",
    "ImportPresetsOut",
    "ImportSessionOut",
    "ImportStartIn",
    "IntegrityOut",
    "InviteIn",
    "InviteOut",
    "JobOut",
    "KnownFormOut",
    "LibraryCreateIn",
    "LibraryDetailOut",
    "LibraryOut",
    "LogEntryOut",
    "LogPageOut",
    "LoginRequest",
    "MediaMetadataOut",
    "MemberOut",
    "MembershipIn",
    "MergeIn",
    "MergePreviewOut",
    "MirrorOut",
    "ModelChoiceOut",
    "MovePlanOut",
    "MoveRequestIn",
    "OcrPageTextOut",
    "OcrTextOut",
    "OffsiteRunOut",
    "OffsiteStatusOut",
    "OffsiteTestOut",
    "PageHitOut",
    "PageOut",
    "PendingReasonOut",
    "PendingReviewOut",
    "PhotoOut",
    "PhotoWallOut",
    "PipelineFilesOut",
    "PipelineStatusOut",
    "QuotaOut",
    "ReclassifyIn",
    "ReclassifyResultOut",
    "RedeemResetIn",
    "RescanResultOut",
    "ResetCodeOut",
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
    "SetupClaimIn",
    "SetupStateOut",
    "SimilarOut",
    "SourceFileDetailOut",
    "SourceFileOut",
    "StageCount",
    "TagOut",
    "TaxonomyHealthOut",
    "TaxonomyOptionOut",
    "TotpConfirmIn",
    "TotpEnrolOut",
    "TotpStatusOut",
    "TreeGroupOut",
    "TreeOut",
    "UnifyApplyIn",
    "UnifyGroupOut",
    "UnifyMemberOut",
    "UnifyProposalOut",
    "UploadResult",
    "UserOut",
    "VaultItemOut",
    "VaultSearchHitOut",
    "VaultSearchOut",
    "VaultStateOut",
    "WhyPanelOut",
]
