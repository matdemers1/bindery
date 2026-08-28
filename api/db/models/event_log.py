import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class EventLog(Base):
    """Diagnostics, persisted and queryable (T-8.11).

    Everything the application logged used to go to container stdout and
    nowhere else, which meant the answer to "why did this file not work" was
    *ssh to the host and read the worker's output* — assuming it had not
    already scrolled away. A `last_error` on the job was the only durable trace,
    truncated, and overwritten by the next attempt.

    So log lines land here as well, tagged with whatever they were about. The
    point is that a failure is explainable from the screen you noticed it on.

    Two properties worth stating:

    **Writes never join the transaction they describe.** A log row that rolled
    back with the failure it was recording would be worse than useless, and a
    logger that can abort the pipeline is worse still. Rows are written by a
    background drain in its own session — see `api/eventlog.py`.

    **`library_id` is set whenever a log line concerns a file.** Log messages
    routinely contain filenames, so the same boundary that governs documents has
    to govern their diagnostics.
    """

    __tablename__ = "event_log"
    __table_args__ = (
        sa.Index("ix_event_log_source_file", "source_file_id", "sequence"),
        sa.Index("ix_event_log_level", "level", "sequence"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # Monotonic, for the same reason the audit trail has one: `created_at`
    # cannot order two rows written in the same instant.
    sequence: Mapped[int] = mapped_column(
        sa.BigInteger, sa.Identity(always=False), nullable=False, unique=True
    )
    level: Mapped[str] = mapped_column(sa.Text, nullable=False)
    logger: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)

    library_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    stage: Mapped[str | None] = mapped_column(sa.Text)

    # The traceback, when there is one. Kept whole rather than truncated: the
    # bottom of a traceback is the useful end and a length cap eats it.
    detail: Mapped[str | None] = mapped_column(sa.Text)
    context: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = created_at()
