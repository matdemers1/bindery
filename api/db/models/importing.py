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
    # Every document this import produces is sealed into its creator's vault
    # as soon as its pipeline finishes, while the vault is open (REQ-197). Set
    # only when the vault was unlocked at creation, so the intent is real.
    to_vault: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    created_at: Mapped[datetime] = created_at()
    # See Document.updated_at: the `import_session_set_updated_at` trigger from
    # migration 0029 maintains this. Only `ImportItem.updated_at` was ever
    # assigned in code, so the session's own clock had stopped at creation.
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


