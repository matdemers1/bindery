import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class OffsiteRun(Base):
    """One attempt to get a copy out of the building (T-13.6, REQ-163).

    Not a `Job`. The queue's idempotency key would make every run after the
    first a silent no-op — see ADR-010, and the migration's docstring.

    Rows accumulate. "When did this last work" is a question about history, and
    a table holding only the current state cannot answer "when did it stop".
    """

    __tablename__ = "offsite_run"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="requested")
    trigger: Mapped[str] = mapped_column(sa.Text, nullable=False, default="schedule")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True))
    started_at: Mapped[datetime] = created_at()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(sa.Text)
    dump_key: Mapped[str | None] = mapped_column(sa.Text)
    dump_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    blobs_uploaded: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    blobs_skipped: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    bytes_sent: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    failures: Mapped[list | None] = mapped_column(sa.JSON)
