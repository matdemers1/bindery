import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum
from api.db.enums import FieldSource as Source


class FieldSource(Base):
    """Who last set each field of a document (T-17.1, REQ-188, REQ-191).

    The table exists to answer one question, asked constantly: **may
    classification write this field?** A field a person set is theirs, and the
    model does not get to quietly disagree with them three days later.

    That question could in principle be derived from `audit_event`, and doing so
    would mean scanning and parsing JSON for every field of every document on
    every classification run. This is the materialised answer; the audit log
    remains the history.

    Not the same thing as `field_provenance`, which hangs off a
    `classification_id` and records the page and snippet that justified an
    AI-written value. Provenance of **evidence** is not provenance of
    **authority** — a document can have excellent evidence for a value a person
    has since overruled.

    One row per `(document_id, field_name)`. A claim given back — by undoing the
    edit that made it — is **released, never deleted**: `released_at` is set and
    the row stays, the same pattern `document_tag.removed_at` and
    `document.superseded_at` use. A live claim is `released_at IS NULL`.

    History of the *values* lives in the audit trail, which is where the rest of
    the project keeps it.
    """

    __tablename__ = "field_source"
    __table_args__ = (
        sa.PrimaryKeyConstraint("document_id", "field_name", name="pk_field_source"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("document.id"), nullable=False, index=True
    )
    # A plain string rather than an enum: the set of editable fields is a
    # product decision that will move, and a migration per field would make
    # adding one a schema change.
    field_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[Source] = mapped_column(
        pg_enum(Source, "field_source_kind"), nullable=False
    )
    # Null for `ai` and `rule` — nobody in particular set them.
    set_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id")
    )
    # What the why-panel links to when it says "a person set this".
    set_by_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("audit_event.id")
    )
    set_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    # Set when an undo walked back the edit that made this claim. The row
    # survives, so "a person set this and then took it back" stays answerable.
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    released_by_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("audit_event.id")
    )
    created_at: Mapped[datetime] = created_at()
