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
from api.db.models.event_log import EventLog, ServiceHeartbeat
from api.db.models.field_source import FieldSource
from api.db.models.importing import (
    ImportItem,
    ImportSession,
)
from api.db.models.job import Job
from api.db.models.known_form import KnownForm
from api.db.models.library import Library
from api.db.models.media import MediaMetadata
from api.db.models.membership import Membership
from api.db.models.oidc import OidcIdentity, OidcLogoutEvent
from api.db.models.offsite_object import OffsiteObject
from api.db.models.offsite_run import OffsiteRun
from api.db.models.page import Page
from api.db.models.setting import Setting
from api.db.models.source_file import SourceFile
from api.db.models.tag import DocumentTag, Tag, live_tag_links
from api.db.models.taxonomy import Correspondent, DocumentType, Rule
from api.db.models.user import (
    AppUser,
    Invitation,
    LoginAttempt,
    PasswordResetCode,
    RecoveryCode,
    RefreshToken,
)
from api.db.models.vault import Vault, VaultItem, VaultPage

__all__ = [
    "ApiToken",
    "AppUser",
    "Asset",
    "AuditEvent",
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
    "FieldSource",
    "ImportItem",
    "ImportSession",
    "Invitation",
    "Job",
    "KnownForm",
    "Library",
    "LoginAttempt",
    "MediaMetadata",
    "Membership",
    "OidcIdentity",
    "OidcLogoutEvent",
    "OffsiteObject",
    "OffsiteRun",
    "Page",
    "PasswordResetCode",
    "RecoveryCode",
    "RefreshToken",
    "Rule",
    "SavedSearch",
    "ServiceHeartbeat",
    "Setting",
    "SourceFile",
    "Tag",
    "Vault",
    "VaultItem",
    "VaultPage",
    "live_tag_links",
]
