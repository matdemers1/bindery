"""Enumerations shared by the models and the baseline migration.

These are native PostgreSQL enum types. Adding a value later is one
`ALTER TYPE … ADD VALUE`; the set of values is deliberately small.
"""

from enum import StrEnum


class LibraryKind(StrEnum):
    PERSONAL = "personal"
    SHARED = "shared"


class MembershipRole(StrEnum):
    OWNER = "owner"
    CONTRIBUTOR = "contributor"
    READER = "reader"


class IngestSource(StrEnum):
    WEB_UPLOAD = "web_upload"
    WATCHED_FOLDER = "watched_folder"
    CAMERA = "camera"
    BULK_IMPORT = "bulk_import"


class SourceFileState(StrEnum):
    """Pipeline position of a source file (Architecture L4)."""

    RECEIVED = "received"
    DUPLICATE = "duplicate"
    NORMALIZING = "normalizing"
    PAGING = "paging"
    SEGMENTING = "segmenting"
    PROCESSED = "processed"
    FAILED = "failed"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    VITAL = "vital"


class Redundancy(StrEnum):
    LOCAL = "local"
    LOCAL_PLUS_OFFSITE = "local_plus_offsite"


class ReviewState(StrEnum):
    FILED = "filed"
    NEEDS_REVIEW = "needs_review"
    PENDING_CLASSIFICATION = "pending_classification"
    FAILED = "failed"


class TagSource(StrEnum):
    """Provenance of a tag, recorded on the link row (Data Model)."""

    AI = "ai"
    RULE = "rule"
    HUMAN = "human"


class JobStage(StrEnum):
    INGEST = "ingest"
    NORMALIZE = "normalize"
    PAGE = "page"
    SEGMENT = "segment"
    EMBED = "embed"
    CLASSIFY = "classify"
    RULES = "rules"
    FILE = "file"
    MIRROR = "mirror"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class ActorType(StrEnum):
    HUMAN = "human"
    AI = "ai"
    RULE = "rule"
    SYSTEM = "system"
