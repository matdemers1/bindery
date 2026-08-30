"""Accounts: invitations, resets, TOTP enrolment, suspension (Phase 10b).

Every function here exists because there is **no email service** (ADR-008). That
one constraint shapes all of it: an invitation is a link the operator sends by
text, a password reset is a code they read out. Out-of-band delivery is
genuinely secure — it just has to be designed for rather than worked around.

The other constraint is ADR-009: an administrator administers *accounts*. There
is deliberately no function in this module that sets another user's password,
reads their documents, or assumes their session. An admin can open the door;
they cannot walk through it.

Secrets are stored as hashes and shown exactly once, like an API token. A value
that can be re-read from the database is a value that leaks with the database.
"""

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.passwords import hash_password, validate_password
from api.auth.totp import verify as verify_totp
from api.db.enums import LibraryKind, MembershipRole
from api.db.models import (
    AppUser,
    Invitation,
    Library,
    Membership,
    PasswordResetCode,
    RecoveryCode,
    RefreshToken,
)

log = logging.getLogger("bindery.accounts")

INVITE_TTL = timedelta(days=14)
RESET_TTL = timedelta(hours=24)
RECOVERY_CODE_COUNT = 10

# 32 bytes of urlsafe base64. Long enough that guessing is not a strategy, short
# enough to survive being pasted into a text message without wrapping.
TOKEN_BYTES = 32


class AccountError(Exception):
    """Refused, with a reason meant for a person to read."""


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(value: str) -> str:
    """SHA-256, not Argon2.

    Argon2 is for passwords, which are short, low-entropy and chosen by people.
    These tokens are 256 bits of `secrets`, so there is no dictionary to defend
    against and nothing for a slow hash to buy — while a slow hash on a lookup
    key would mean rehashing to find the row.
    """
    return hashlib.sha256(value.encode()).hexdigest()


# --------------------------------------------------------------------------
# Invitations (REQ-139, REQ-140)
# --------------------------------------------------------------------------


@dataclass
class IssuedInvitation:
    invitation: Invitation
    token: str  # shown once, never stored


async def invite(
    session: AsyncSession,
    *,
    email: str,
    library_name: str,
    created_by: AppUser,
    storage_quota_bytes: int | None = None,
    note: str | None = None,
) -> IssuedInvitation:
    """Create a single-use invitation. Returns the token exactly once."""
    email = email.strip().lower()
    if not email or "@" not in email:
        raise AccountError("that does not look like an email address")

    existing = (
        await session.execute(sa.select(AppUser).where(AppUser.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise AccountError("an account with that address already exists")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    invitation = Invitation(
        email=email,
        token_hash=_hash(token),
        library_name=library_name.strip() or "Documents",
        storage_quota_bytes=storage_quota_bytes,
        note=note,
        created_by_id=created_by.id,
        expires_at=_now() + INVITE_TTL,
    )
    session.add(invitation)
    await session.flush()
    log.info("invited %s, expiring %s", email, invitation.expires_at.date())
    return IssuedInvitation(invitation=invitation, token=token)


async def find_invitation(session: AsyncSession, token: str) -> Invitation:
    """Resolve a token to a usable invitation, or say why not."""
    invitation = (
        await session.execute(
            sa.select(Invitation).where(Invitation.token_hash == _hash(token or ""))
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise AccountError("that invitation link is not valid")
    if invitation.revoked_at is not None:
        raise AccountError("that invitation was withdrawn")
    if invitation.accepted_at is not None:
        raise AccountError("that invitation has already been used")
    if invitation.expires_at <= _now():
        raise AccountError("that invitation has expired")
    return invitation


async def accept(
    session: AsyncSession, *, token: str, password: str, display_name: str | None
) -> AppUser:
    """Turn an invitation into an account, its library, and its membership.

    One account, one personal library, owner of it and of nothing else. The
    acceptor chooses their password and their display name; everything else —
    the address, the library name, the quota — was decided by whoever invited
    them and is not theirs to change here.
    """
    invitation = await find_invitation(session, token)
    validate_password(password, email=invitation.email)

    user = AppUser(
        email=invitation.email,
        password_hash=hash_password(password),
        display_name=(display_name or "").strip() or None,
        storage_quota_bytes=invitation.storage_quota_bytes,
    )
    library = Library(name=invitation.library_name, kind=LibraryKind.PERSONAL)
    session.add_all([user, library])
    await session.flush()

    session.add(
        Membership(user_id=user.id, library_id=library.id, role=MembershipRole.OWNER)
    )
    invitation.accepted_at = _now()
    invitation.accepted_user_id = user.id
    await session.flush()
    log.info("invitation accepted by %s", user.email)
    return user


# --------------------------------------------------------------------------
# Passwords (REQ-136, REQ-137)
# --------------------------------------------------------------------------


async def revoke_other_sessions(
    session: AsyncSession, user: AppUser, *, keep: uuid.UUID | None = None
) -> int:
    """End every session but, optionally, the one making the request.

    A password change that leaves old sessions alive changes nothing for
    whoever already had one, which is the case the change is usually being made
    for (REQ-137).
    """
    condition = [RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)]
    if keep is not None:
        condition.append(RefreshToken.id != keep)
    result = await session.execute(
        sa.update(RefreshToken).where(sa.and_(*condition)).values(revoked_at=_now())
    )
    return result.rowcount or 0


async def issue_reset_code(
    session: AsyncSession, *, user: AppUser, issued_by: AppUser
) -> str:
    """A one-time code for an administrator to hand over. Returned once.

    Deliberately not a password. The administrator opens the door; the account
    holder chooses what is behind it, so an admin never knows a password that
    is not theirs and cannot later be unable to prove it.
    """
    # Groups of four, because this gets read aloud over a phone.
    raw = "-".join(
        "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(4))
        for _ in range(3)
    )
    session.add(
        PasswordResetCode(
            user_id=user.id,
            code_hash=_hash(raw),
            issued_by_id=issued_by.id,
            expires_at=_now() + RESET_TTL,
        )
    )
    await session.flush()
    log.info("reset code issued for %s by %s", user.email, issued_by.email)
    return raw


async def redeem_reset_code(
    session: AsyncSession, *, email: str, code: str, new_password: str
) -> AppUser:
    """Spend a reset code and set the password it unlocked."""
    email = email.strip().lower()
    user = (
        await session.execute(sa.select(AppUser).where(AppUser.email == email))
    ).scalar_one_or_none()
    reset = (
        await session.execute(
            sa.select(PasswordResetCode).where(
                PasswordResetCode.code_hash == _hash((code or "").strip().upper())
            )
        )
    ).scalar_one_or_none()

    # One message for every way this can fail, for the same reason the login
    # form has one: otherwise it reports whether an address exists (REQ-135).
    if (
        user is None
        or reset is None
        or reset.user_id != user.id
        or reset.used_at is not None
        or reset.expires_at <= _now()
    ):
        raise AccountError("that code is not valid")

    validate_password(new_password, email=email)
    user.password_hash = hash_password(new_password)
    user.locked_until = None
    reset.used_at = _now()
    await revoke_other_sessions(session, user)
    await session.flush()
    log.info("password reset completed for %s", user.email)
    return user


async def change_password(
    session: AsyncSession, *, user: AppUser, new_password: str
) -> None:
    validate_password(new_password, email=user.email)
    user.password_hash = hash_password(new_password)
    await revoke_other_sessions(session, user)
    await session.flush()


# --------------------------------------------------------------------------
# TOTP (REQ-138, REQ-156)
# --------------------------------------------------------------------------


async def new_recovery_codes(session: AsyncSession, user: AppUser) -> list[str]:
    """Replace this user's recovery codes. Returned once."""
    await session.execute(
        sa.delete(RecoveryCode).where(
            RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
        )
    )
    codes = [
        "-".join(
            "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(5))
            for _ in range(2)
        )
        for _ in range(RECOVERY_CODE_COUNT)
    ]
    for code in codes:
        session.add(RecoveryCode(user_id=user.id, code_hash=_hash(code)))
    await session.flush()
    return codes


async def confirm_totp(session: AsyncSession, *, user: AppUser, code: str) -> list[str]:
    """Activate a secret by proving a code from it. Returns recovery codes.

    Nothing is trusted until this succeeds: a secret generated and stored as
    active, before the account holder has ever produced a code from it, locks
    them out of their own archive if the enrolment did not take.
    """
    if not user.totp_secret:
        raise AccountError("start enrolment first")
    step = verify_totp(user.totp_secret, code, after_step=user.totp_last_step)
    if step is None:
        raise AccountError("that code is not right — check the time on your device")
    user.totp_confirmed_at = _now()
    user.totp_last_step = step
    codes = await new_recovery_codes(session, user)
    log.info("totp enrolled for %s", user.email)
    return codes


async def check_second_factor(
    session: AsyncSession, *, user: AppUser, code: str
) -> None:
    """Verify a TOTP code or spend a recovery code. Raises on failure."""
    if not user.totp_enabled:
        return
    step = verify_totp(user.totp_secret or "", code, after_step=user.totp_last_step)
    if step is not None:
        user.totp_last_step = step
        return

    normalised = (code or "").strip().lower()
    recovery = (
        await session.execute(
            sa.select(RecoveryCode).where(
                RecoveryCode.user_id == user.id,
                RecoveryCode.code_hash == _hash(normalised),
                RecoveryCode.used_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if recovery is None:
        raise AccountError("that code is not right")
    recovery.used_at = _now()
    log.warning("recovery code spent for %s", user.email)


async def grant_admin(session: AsyncSession, *, user: AppUser) -> None:
    """Make an account an administrator. Refused without TOTP (REQ-156).

    An administrator can issue a reset code for every other account, so that
    account is the master key to the box. Optional two-factor is the operator's
    decision for ordinary accounts; it is not available for this one, and the
    refusal lives here rather than only in the UI so it cannot be clicked past.
    """
    if not user.totp_enabled:
        raise AccountError(
            "enrol two-factor authentication before taking administrator rights: "
            "an administrator can reset every other password"
        )
    user.is_admin = True
    await session.flush()
    log.warning("admin rights granted to %s", user.email)


# --------------------------------------------------------------------------
# Suspension (REQ-145)
# --------------------------------------------------------------------------


async def suspend(session: AsyncSession, *, user: AppUser, by: AppUser) -> int:
    """Stop the sessions. Keep every byte (REQ-090)."""
    if user.id == by.id:
        raise AccountError("you cannot suspend your own account")
    user.suspended_at = _now()
    user.suspended_by_id = by.id
    user.is_active = False
    revoked = await revoke_other_sessions(session, user)
    await session.flush()
    log.warning("suspended %s (%s sessions ended)", user.email, revoked)
    return revoked


async def restore(session: AsyncSession, *, user: AppUser) -> None:
    user.suspended_at = None
    user.suspended_by_id = None
    user.is_active = True
    await session.flush()
    log.info("restored %s", user.email)
