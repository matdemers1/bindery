import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class OidcIdentity(Base):
    """A Bindery account's link to an identity at an OpenID Connect provider (Phase 20).

    **The key is `(issuer, subject)` and nothing else.** Not email: an address
    is a display value that changes, is reused, and at some providers is chosen
    by the person claiming it — matching on it is account takeover wearing the
    costume of convenience. The consumer contract says the same thing, and both
    SDKs deliberately expose no way to look anybody up by email.

    One link per account, one account per link. The uniqueness is declared twice
    because the two directions fail differently: a second identity on one
    account would make "who is signed in" ambiguous, and one identity on two
    accounts would make a revoke at the provider end the wrong session.
    """

    __tablename__ = "oidc_identity"
    __table_args__ = (
        sa.UniqueConstraint("issuer", "subject", name="uq_oidc_identity_issuer_subject"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False, unique=True
    )
    issuer: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # What the provider calls them, for the Settings card to show. A label, and
    # never a key — `preferred_username` is documented as stable, and this code
    # still refuses to match on it.
    preferred_username: Mapped[str | None] = mapped_column(sa.Text)

    # The provider's refresh token, Fernet-encrypted like every other secret
    # this application stores. It buys one thing: re-reading the roles when
    # Bindery renews its own session, so a grant withdrawn upstream lands here
    # without waiting for the person to sign in again (REQ-207).
    refresh_token_enc: Mapped[str | None] = mapped_column(sa.Text)

    # How the link came to exist: `sso` for an account provisioned on first
    # sign-in, `link` for one an existing account connected deliberately. The
    # audit trail says the same, and this is what the Settings card reads.
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="link")

    linked_at: Mapped[datetime] = created_at()
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class OidcLogoutEvent(Base):
    """A back-channel logout already applied, remembered by `jti` (REQ-208).

    Delivery retries — three attempts — so the second arrival of an event must
    not end a session the person has since started again. The SDK ships an
    in-memory store and says in its own docstring that it is enough for one
    process; this is the shared one, in the database both the api and anything
    that replaces it already talk to.

    Not pruned by anything unattended (invariant 3). A row is a few dozen bytes
    and the table grows with sign-outs, not with the archive.
    """

    __tablename__ = "oidc_logout_event"

    jti: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: The provider session the event named, when it named one.
    sid: Mapped[str | None] = mapped_column(sa.Text)
    received_at: Mapped[datetime] = created_at()
    #: How many Bindery sessions it ended. Zero is normal and not a failure.
    ended_sessions: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
