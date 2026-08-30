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
    # Set when repeated failures locked the account, cleared by a successful
    # login or by an administrator. Short and self-expiring on purpose: a
    # permanent lockout anyone can trigger by guessing badly is a weapon, not a
    # defence (see api/auth/throttle.py).
    locked_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    # Administers *accounts*, never documents. There is deliberately no admin
    # branch in `visible_library_ids` — see ADR-009. An administrator can invite,
    # suspend, set quotas and read the pipeline; they cannot open anyone else's
    # page, and the permission suite asserts it.
    is_admin: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    # Suspension keeps the data and stops the sessions (REQ-145, REQ-090).
    # Distinct from `is_active`, which predates this and is the same switch by
    # another name; suspension additionally records who and when.
    suspended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    suspended_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id")
    )

    # Bytes of original blobs this account may hold. NULL means no limit, which
    # is the owner's own account; guests get a number at invite time (REQ-141).
    storage_quota_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)

    # TOTP. `confirmed_at` is what makes it active: a secret is generated when
    # enrolment starts and must not be trusted until a code from it has been
    # verified, or a failed enrolment locks the account out of itself.
    totp_secret: Mapped[str | None] = mapped_column(sa.Text)
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # The last 30-second step a code was accepted for. Without it a code stays
    # valid for the whole ±1-step window — up to 90 seconds — which is ample
    # time to reuse one that was shoulder-surfed or captured.
    totp_last_step: Mapped[int | None] = mapped_column(sa.BigInteger)

    created_at: Mapped[datetime] = created_at()

    @property
    def totp_enabled(self) -> bool:
        return self.totp_secret is not None and self.totp_confirmed_at is not None

    @validates("email")
    def _normalise_email(self, _key: str, value: str) -> str:
        """One place, so every write path agrees with every lookup."""
        return value.strip().lower()


class LoginAttempt(Base):
    """One attempt at the login form, successful or not (REQ-133, REQ-144).

    Kept rather than counted-and-discarded because "was anyone trying?" is a
    question you only think to ask afterwards. The email is stored as typed —
    lowercased — including addresses that do not exist, which is what makes a
    password-spray against invented names visible at all.
    """

    __tablename__ = "login_attempt"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(sa.Text, index=True)
    succeeded: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    created_at: Mapped[datetime] = created_at()


class Invitation(Base):
    """A single-use, expiring link that creates one account (REQ-139, REQ-140).

    There is no email service (ADR-008), so an invitation is a link the operator
    sends by text or hands over in person. Out-of-band delivery is genuinely
    secure — it just has to be designed for rather than worked around, which is
    why the token is long, single-use, and expires.

    Only the hash is stored. The link is shown once, at creation, exactly like
    an API token.
    """

    __tablename__ = "invitation"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    # What the accepted account will get. Recorded on the invitation so the
    # operator decides at invite time and the acceptor cannot choose.
    library_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    storage_quota_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    note: Mapped[str | None] = mapped_column(sa.Text)

    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()

    @validates("email")
    def _normalise_email(self, _key: str, value: str) -> str:
        return value.strip().lower()


class PasswordResetCode(Base):
    """A one-time code an administrator hands over out of band (REQ-136).

    No email service means no reset link, so the operator generates a code and
    reads it out. The important half is what this is *not*: there is no endpoint
    anywhere that lets an administrator set another user's password. They can
    open the door; they cannot walk through it and then be unable to prove they
    did not.
    """

    __tablename__ = "password_reset_code"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    issued_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class RecoveryCode(Base):
    """One unused TOTP recovery code.

    Rows rather than a column, so using one is a single update and the count
    remaining is a query. Only hashes are stored, and a used code is marked
    rather than deleted — "which of my codes have I spent" is answerable.
    """

    __tablename__ = "recovery_code"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


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
