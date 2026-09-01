import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum
from api.db.enums import MediaKind


class MediaMetadata(Base):
    """What a photograph or video already knows about itself (REQ-193, REQ-194).

    A picture knows when and where it was taken and on what; a video knows how
    long it is and how big. The archive used to discard all of it and ask the
    model to guess. One row per source file, written by the worker when it
    first reads the file, and never rewritten by anything that did not read
    the file again.

    The normalised columns are the ones screens sort and filter on; `raw`
    keeps everything the probe returned, so a field nobody thought to promote
    is still there when somebody wants it.
    """

    __tablename__ = "media_metadata"

    source_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("source_file.id"), primary_key=True
    )
    kind: Mapped[MediaKind] = mapped_column(pg_enum(MediaKind, "media_kind"), nullable=False)
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    duration_seconds: Mapped[float | None] = mapped_column(sa.Float)
    captured_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    camera_make: Mapped[str | None] = mapped_column(sa.Text)
    camera_model: Mapped[str | None] = mapped_column(sa.Text)
    latitude: Mapped[float | None] = mapped_column(sa.Float)
    longitude: Mapped[float | None] = mapped_column(sa.Float)
    codec: Mapped[str | None] = mapped_column(sa.Text)
    frame_rate: Mapped[float | None] = mapped_column(sa.Float)
    # Whether a browser will play it as-is. False for .avi, .mkv, .wmv — those
    # are stored and downloadable, and the screen says so instead of showing a
    # player that never starts.
    browser_playable: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = created_at()
