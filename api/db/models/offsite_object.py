import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class OffsiteObject(Base):
    """One object known to be in the offsite bucket (T-13.4, REQ-161).

    A ledger, not a source of truth. It exists so a nightly run can skip the
    495 blobs it already shipped without 495 round trips — but it cannot know
    the bucket was emptied by hand, so `verified_at` records when the two were
    last actually compared, and the weekly reconcile is what compares them.

    Nothing here is ever deleted. A row whose object has gone missing gets
    `verified_at` cleared and is re-uploaded; the row is the record that it was
    once shipped, which is worth keeping even when it turns out not to be true
    any more.
    """

    __tablename__ = "offsite_object"

    id: Mapped[uuid.UUID] = uuid_pk()
    object_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    sha256: Mapped[str | None] = mapped_column(sa.Text, index=True)
    byte_size: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    uploaded_at: Mapped[datetime] = created_at()
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
