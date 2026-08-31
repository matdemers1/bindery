import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import JobStage, JobState


class Job(Base):
    """The queue and the observability surface (ADR-002).

    Consumed with SELECT … FOR UPDATE SKIP LOCKED. "What is stuck and why" is a
    SQL query against this table, and the health panel reads it directly.

    The queue *runner* is Phase 1 (T-1.1); this is the table it will claim from.
    """

    __tablename__ = "job"
    __table_args__ = (
        # Idempotency key (Data Model). document_id is null for file-scoped
        # stages and set for per-document stages such as classify, so the
        # uniqueness has to treat those nulls as equal.
        sa.UniqueConstraint(
            "source_file_id",
            "document_id",
            "stage",
            "prompt_version",
            postgresql_nulls_not_distinct=True,
        ),
        sa.Index("ix_job_state_scheduled_for", "state", "scheduled_for"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("source_file.id"), index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("document.id"), index=True
    )
    stage: Mapped[JobStage] = mapped_column(pg_enum(JobStage, "job_stage"), nullable=False)
    state: Mapped[JobState] = mapped_column(
        pg_enum(JobState, "job_state"),
        nullable=False,
        server_default=JobState.QUEUED.value,
    )
    # Written to every job so "reprocess everything still on v2" is a query.
    prompt_version: Mapped[str | None] = mapped_column(sa.Text)

    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(sa.Text)

    # Set when a person says "I have seen this". It does not retry, hide or
    # remove the job — the row and its error stay on the Pipeline screen — it
    # only stops the job counting as work still demanding attention (ADR-011).
    acknowledged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    scheduled_for: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
