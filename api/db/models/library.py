import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import LibraryKind


class Library(Base):
    """The access boundary (ADR-005, REQ-099).

    Every document belongs to exactly one library. Permission filtering happens
    against the caller's visible library set at the repository layer.
    """

    __tablename__ = "library"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[LibraryKind] = mapped_column(
        pg_enum(LibraryKind, "library_kind"), nullable=False
    )
    created_at: Mapped[datetime] = created_at()
