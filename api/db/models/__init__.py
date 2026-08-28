"""All ORM models. Importing this package registers every table on Base.metadata."""

from api.db.models.api_token import ApiToken
from api.db.models.audit_event import AuditEvent
from api.db.models.classification import Classification, FieldProvenance
from api.db.models.document import Document
from api.db.models.entities import (
    Asset,
    CorrespondentAlias,
    DocumentAsset,
    DuplicatePair,
    SavedSearch,
)
from api.db.models.event_log import EventLog
from api.db.models.importing import (
    BatchRequest,
    BatchSubmission,
    ImportItem,
    ImportSession,
)
from api.db.models.job import Job
from api.db.models.known_form import KnownForm
from api.db.models.library import Library
from api.db.models.membership import Membership
from api.db.models.page import Page
from api.db.models.setting import Setting
from api.db.models.source_file import SourceFile
from api.db.models.tag import DocumentTag, Tag, live_tag_links
from api.db.models.taxonomy import Correspondent, DocumentType, Rule
from api.db.models.user import AppUser, RefreshToken

__all__ = [
    "ApiToken",
    "AppUser",
    "Asset",
    "AuditEvent",
    "BatchRequest",
    "BatchSubmission",
    "Classification",
    "Correspondent",
    "CorrespondentAlias",
    "Document",
    "DocumentAsset",
    "DocumentTag",
    "DocumentType",
    "DuplicatePair",
    "EventLog",
    "FieldProvenance",
    "ImportItem",
    "ImportSession",
    "Job",
    "KnownForm",
    "Library",
    "Membership",
    "Page",
    "RefreshToken",
    "Rule",
    "SavedSearch",
    "Setting",
    "SourceFile",
    "Tag",
    "live_tag_links",
]
