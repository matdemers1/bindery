import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import MembershipRole


class Membership(Base):
    """Which users can see which libraries, and with what authority."""

    __tablename__ = "membership"
    __table_args__ = (sa.UniqueConstraint("user_id", "library_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False, index=True
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), nullable=False, index=True
    )
    role: Mapped[MembershipRole] = mapped_column(
        pg_enum(MembershipRole, "membership_role"), nullable=False
    )
    created_at: Mapped[datetime] = created_at()
