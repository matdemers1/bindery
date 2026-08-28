import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import TagSource


class Tag(Base):
    """Taxonomy. Reuse resolves by id and never through normalisation (invariant 6)."""

    __tablename__ = "tag"
    __table_args__ = (
        # NULLS NOT DISTINCT so a library-less (global) tag slug is still unique.
        sa.UniqueConstraint("library_id", "slug", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # NULL means the tag is shared across every library.
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), index=True
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False)
    color: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id")
    )
    created_at: Mapped[datetime] = created_at()


class DocumentTag(Base):
    """Link row carrying the provenance of the tag (REQ-078).

    `source` is what lets the why-panel distinguish "the model chose this" from
    "your rule forced this" from "you set this".

    Links are **superseded, never deleted** — the same answer this project gives
    everywhere else. Undoing a classification takes back the tags it applied
    without erasing the fact that it applied them.
    """

    __tablename__ = "document_tag"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("document.id"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("tag.id"), primary_key=True
    )
    source: Mapped[TagSource] = mapped_column(
        pg_enum(TagSource, "tag_source"), nullable=False
    )
    created_at: Mapped[datetime] = created_at()
    # Set when an undo took the link back. Live links are `removed_at IS NULL`.
    removed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    removed_by_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("audit_event.id")
    )


def live_tag_links() -> sa.ColumnElement[bool]:
    """The filter every read of a document's tags must apply.

    A superseded link is history: it records that a classifier once applied this
    tag and that an undo took it back.
    """
    return DocumentTag.removed_at.is_(None)
