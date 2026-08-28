import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from api.db.base import Base, created_at, uuid_pk


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(sa.Text)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = created_at()

    @validates("email")
    def _normalise_email(self, _key: str, value: str) -> str:
        """One place, so every write path agrees with every lookup."""
        return value.strip().lower()


class RefreshToken(Base):
    """Server-side record of an issued refresh token (REQ-103).

    Rotation needs server-side state, so this table is not in the Data Model doc
    — it is an auth implementation detail rather than archive data.

    Only the hash is stored. Rotation *revokes* rather than deletes, both because
    nothing in Bindery deletes on its own (invariant 3) and because the chain is
    what makes token-reuse detection possible.
    """

    __tablename__ = "refresh_token"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    issued_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # Set when this token was rotated, forming the chain reuse detection walks.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("refresh_token.id")
    )
