import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import IngestSource, SourceFileState


class SourceFile(Base):
    """An immutable original (invariant 1, ADR-001).

    Bytes live at /data/blobs/<aa>/<bb>/<sha256> and are never modified, and
    identical bytes are one file on disk however many libraries record holding
    them — that is what content-addressing buys.

    The uniqueness is on **(library_id, sha256)**, not on sha256 alone. Global
    uniqueness was right while there was one account and became a cross-tenant
    leak the moment there could be two: an upload would match another
    household's row, hand back their filename in the response, and quietly not
    file your copy in your own library.
    """

    __tablename__ = "source_file"
    __table_args__ = (
        sa.UniqueConstraint("library_id", "sha256", name="uq_source_file_library_sha256"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # The library this file was ingested into; documents sliced from it inherit
    # it. The access boundary itself is document.library_id (ADR-005) — this
    # column exists so a file with no documents yet still has an owner, which
    # blob serving needs (REQ-106).
    library_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), nullable=False, index=True
    )
    sha256: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(sa.Text)
    original_filename: Mapped[str | None] = mapped_column(sa.Text)
    ingest_source: Mapped[IngestSource] = mapped_column(
        pg_enum(IngestSource, "ingest_source"), nullable=False
    )
    ingest_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    page_count: Mapped[int | None] = mapped_column(sa.Integer)
    state: Mapped[SourceFileState] = mapped_column(
        pg_enum(SourceFileState, "source_file_state"),
        nullable=False,
        server_default=SourceFileState.RECEIVED.value,
        index=True,
    )
    received_at: Mapped[datetime] = created_at()
