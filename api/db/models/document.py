import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import Redundancy, ReviewState, Sensitivity


class Document(Base):
    """A page range over an immutable source file (ADR-001, invariant 2).

    There is no separate segment table. A single-document scan is pages 1..N; a
    100-page bundle produces thirty rows over one file. Wrong boundaries are a
    metadata edit — the original bytes are never touched.

    Correspondent, document type, known form and embedding columns are added in
    Phases 3 and 5, when the tables they point at exist.
    """

    __tablename__ = "document"
    __table_args__ = (
        sa.CheckConstraint("page_start >= 1", name="page_start_positive"),
        sa.CheckConstraint("page_end >= page_start", name="page_range_ordered"),
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
    # Keeps a 5,000-document backlog import out of the daily review queue.
    is_backlog: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
