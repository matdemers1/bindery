import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class ApiToken(Base):
    """A scoped, revocable credential for scripts and the scanner (REQ-107).

    Three properties, each load-bearing:

    **Only the hash is stored.** The token itself is shown once, at creation,
    and is unrecoverable afterwards. A stolen database backup should not be a
    set of working credentials.

    **Scopes are a closed set, and a token can never exceed its creator.**
    Library access is intersected with the creating user's memberships at every
    request, so revoking someone's membership revokes their tokens' reach too —
    without having to find the tokens.

    **Revoked, never deleted.** `revoked_at` keeps the audit trail of what
    existed and what it could do (REQ-090).
    """

    __tablename__ = "api_token"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # SHA-256 of the token. Not a password hash: the token is 256 bits of
    # randomness, so there is nothing to brute-force and nothing to salt.
    token_hash: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    # The first characters, so a person can tell two tokens apart in a list.
    prefix: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), nullable=False, server_default="{}"
    )
    library_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
