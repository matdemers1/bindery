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

    **`user_id` is set whenever a log line came from somebody's request.** Some
    lines legitimately have no library — an import scan naming a folder, an
    auth failure — and those were globally visible, which is a leak of a
    different shape: not a document, but a path, an address, or a question
    somebody typed. An unbound line is now shown to the person who caused it,
    or, when nothing caused it, only if its logger is on the operational
    allowlist (REQ-144).

    **This table is deliberately never pruned**, and that is a decision with a
    cost attached: rows accumulate for the life of the deployment — seven
    stages over a twenty-thousand-file backlog is ~280K of them from the
    start/finish lines alone — while the log viewer's search is
    `message ILIKE '%q%'`, which no ordinary index can serve. The answer is the
    trigram index below, not a scheduled delete: invariant 3 governs documents
    and originals, but a job that removes rows on a timer is the one shape of
    unattended destruction this codebase does not have, and diagnostics are not
    worth introducing it for.
    """

    __tablename__ = "event_log"
    __table_args__ = (
        sa.Index("ix_event_log_source_file", "source_file_id", "sequence"),
        sa.Index("ix_event_log_level", "level", "sequence"),
        # The log viewer's `q`. `pg_trgm` is installed by migration 0001.
        sa.Index(
            "ix_event_log_message_trgm",
            "message",
            postgresql_using="gin",
            postgresql_ops={"message": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # Monotonic, for the same reason the audit trail has one: `created_at`
    # cannot order two rows written in the same instant.
    sequence: Mapped[int] = mapped_column(
        sa.BigInteger, sa.Identity(always=False), nullable=False, unique=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), index=True
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


class ServiceHeartbeat(Base):
    """What each service is running, and when it last said so (REQ-153).

    One row per service, upserted. The api can report its own build from its own
    environment; the worker is a separate image, pulled separately, and can be a
    different commit without anything looking wrong — the queue drains, the
    screens render, and a stage quietly behaves like last week.
    """

    __tablename__ = "service_heartbeat"

    id: Mapped[uuid.UUID] = uuid_pk()
    service: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    commit: Mapped[str] = mapped_column(sa.Text, nullable=False)
    built_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    ref: Mapped[str] = mapped_column(sa.Text, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
