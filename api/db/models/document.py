import uuid
from datetime import date, datetime

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import Redundancy, ReviewState, Sensitivity
from api.embedding import EMBEDDING_DIMENSIONS


class Document(Base):
    """A page range over an immutable source file (ADR-001, invariant 2).

    There is no separate segment table. A single-document scan is pages 1..N; a
    100-page bundle produces thirty rows over one file. Wrong boundaries are a
    metadata edit — the original bytes are never touched.

    Correspondent and document-type columns arrived in Phase 3; asset links
    arrive in Phase 5.

    **Segments are superseded, never deleted.** Re-segmenting a bundle would
    otherwise mean destroying document rows, which invariant 3 forbids and which
    would make undo (REQ-037) a reconstruction rather than a flip. Live segments
    are the rows with `superseded_at IS NULL`; everything else is history.
    """

    __tablename__ = "document"
    __table_args__ = (
        sa.CheckConstraint("page_start >= 1", name="page_start_positive"),
        sa.CheckConstraint("page_end >= page_start", name="page_range_ordered"),
        # REQ-033: a document's library must be its source file's library. A
        # composite foreign key enforces it declaratively — no trigger, and no
        # call site that can forget.
        sa.ForeignKeyConstraint(
            ["source_file_id", "library_id"],
            ["source_file.id", "source_file.library_id"],
            name="fk_document_source_file_library",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # The access boundary (REQ-099). Exactly one library, never null.
    library_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), nullable=False, index=True
    )
    source_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("source_file.id"), nullable=False, index=True
    )
    page_start: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    title: Mapped[str | None] = mapped_column(sa.Text)
    summary: Mapped[str | None] = mapped_column(sa.Text)
    document_date: Mapped[date | None] = mapped_column(sa.Date, index=True)

    # Tier columns exist from the first migration (REQ-089): retrofitting a
    # classification scheme onto already-filed documents means re-touching every
    # one of them.
    sensitivity: Mapped[Sensitivity] = mapped_column(
        pg_enum(Sensitivity, "sensitivity"),
        nullable=False,
        server_default=Sensitivity.NORMAL.value,
        index=True,
    )
    redundancy: Mapped[Redundancy] = mapped_column(
        pg_enum(Redundancy, "redundancy"),
        nullable=False,
        server_default=Redundancy.LOCAL.value,
    )
    review_state: Mapped[ReviewState] = mapped_column(
        pg_enum(ReviewState, "review_state"),
        nullable=False,
        server_default=ReviewState.PENDING_CLASSIFICATION.value,
        index=True,
    )
    # A registry match is a fact, not an opinion (REQ-038).
    known_form_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("known_form.id"), index=True
    )
    # Soft, model-suggested. Reused by id, never by name (invariant 6).
    document_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("document_type.id"), index=True
    )
    correspondent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("correspondent.id"), index=True
    )

    # Lexical embedding for neighbour retrieval, find-similar and Q&A. Not used
    # for search — semantic search was deliberately declined (ADR-004).
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))

    # Keeps a 5,000-document backlog import out of the daily review queue.
    is_backlog: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    # Set when a re-segmentation replaced this row. The exclusion constraint on
    # overlapping page ranges applies only to live rows, so history can overlap
    # freely while the current set stays coherent.
    # Set when this document is in someone's private vault (ADR-012). The
    # repository layer refuses to return a row carrying it without an unlocked
    # session for that user — scoped there rather than at each call site, for
    # the reason ADR-005 gives about boundaries that get forgotten.
    vaulted_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("app_user.id")
    )
    superseded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # The audit event that superseded it — what `undo` walks back.
    superseded_by_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("audit_event.id")
    )

    created_at: Mapped[datetime] = created_at()
    # Maintained by the `document_set_updated_at` trigger (migration 0029), not
    # by `onupdate=`: half the writes that matter here are bulk
    # `sa.update(Document)` statements that never load an object, and a
    # client-side default would leave exactly those silently stale.
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
