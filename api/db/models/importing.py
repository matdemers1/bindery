import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import ImportItemState, ImportState


class ImportSession(Base):
    """One run of the backlog importer.

    > Backlog import floods the review queue; triage becomes the new mess and the
    > project is abandoned.

    That is **R-03**, rated the most likely abandonment point in the whole plan.
    Everything about this table's shape follows from it: the run is staged so a
    human sees counts and a cost before anything happens, it is resumable so an
    interruption is not a restart, and its documents are flagged so they never
    reach the daily queue.
    """

    __tablename__ = "import_session"

    id: Mapped[uuid.UUID] = uuid_pk()
    library_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), nullable=False, index=True
    )
    root_path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[ImportState] = mapped_column(
        pg_enum(ImportState, "import_state"), nullable=False,
        server_default=ImportState.SCANNING.value,
    )
    # Pass one classifies a sample; pass two runs the rest against the taxonomy
    # a human curated in between (REQ-084).
    pass_number: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    sample_size: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="200")
    dry_run: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    cost_estimate: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id")
    )


class ImportItem(Base):
    """One file the walker found. Idempotent per path (REQ-086)."""

    __tablename__ = "import_item"
    __table_args__ = (sa.UniqueConstraint("session_id", "path"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("import_session.id"), nullable=False
    )
    path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    byte_size: Mapped[int | None] = mapped_column(sa.BigInteger)
    sha256: Mapped[str | None] = mapped_column(sa.Text)
    state: Mapped[ImportItemState] = mapped_column(
        pg_enum(ImportItemState, "import_item_state"), nullable=False,
        server_default=ImportItemState.PENDING.value,
    )
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("source_file.id")
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class BatchSubmission(Base):
    """A Batch API submission — half price, and the right tool for a backlog."""

    __tablename__ = "batch_submission"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("import_session.id")
    )
    provider_batch_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="submitted")
    request_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    succeeded_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    errored_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    usage: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class BatchRequest(Base):
    """One document inside a batch.

    **Results arrive in any order.** `custom_id` is the only safe key — matching
    by position silently attaches one document's classification to another, which
    is the worst possible failure in a system whose kill criterion is trust.
    """

    __tablename__ = "batch_request"
    __table_args__ = (sa.UniqueConstraint("submission_id", "custom_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("batch_submission.id"), nullable=False
    )
    custom_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("document.id"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="submitted")
    error: Mapped[str | None] = mapped_column(sa.Text)
