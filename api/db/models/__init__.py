"""All ORM models. Importing this package registers every table on Base.metadata."""

from api.db.models.audit_event import AuditEvent
from api.db.models.classification import Classification, FieldProvenance
from api.db.models.document import Document
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
    "AppUser",
    "AuditEvent",
    "Classification",
    "Correspondent",
    "Document",
    "DocumentTag",
    "DocumentType",
    "FieldProvenance",
    "Job",
    "KnownForm",
    "Library",
    "Membership",
    "Page",
    "RefreshToken",
    "Rule",
    "Setting",
    "SourceFile",
    "Tag",
    "live_tag_links",
]
