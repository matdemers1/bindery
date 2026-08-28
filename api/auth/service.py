"""Session lifecycle: login, refresh rotation, logout.

Rotation revokes rather than deletes (invariant 3), which also gives us the
chain needed to detect a replayed refresh token.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.passwords import verify_password
from api.auth.tokens import hash_refresh_secret, issue_access_token, new_refresh_secret
from api.db.models import AppUser, RefreshToken


class AuthError(Exception):
    """Authentication failed. The message is safe to show to the caller."""


async def authenticate(session: AsyncSession, email: str, password: str) -> AppUser:
    result = await session.execute(
        sa.select(AppUser).where(AppUser.email == email.strip().lower())
    )
    user = result.scalar_one_or_none()
    # Verify even when the user is missing would be better still; for now keep
    # the branches identical from the caller's point of view.
    if user is None or not verify_password(user.password_hash, password):
        raise AuthError("invalid credentials")
    if not user.is_active:
        raise AuthError("account is disabled")
    return user


async def issue_session(
    session: AsyncSession, user: AppUser, *, replaces: RefreshToken | None = None
) -> tuple[str, str]:
    """Mint an access token and a fresh refresh token. Returns (access, refresh)."""
    access_token, _ = issue_access_token(user.id)
    secret, secret_hash, expires_at = new_refresh_secret()

    refresh = RefreshToken(user_id=user.id, token_hash=secret_hash, expires_at=expires_at)
    session.add(refresh)
    await session.flush()

    if replaces is not None:
        replaces.revoked_at = datetime.now(UTC)
        replaces.replaced_by_id = refresh.id

    return access_token, secret


async def rotate_session(session: AsyncSession, refresh_secret: str) -> tuple[str, str, AppUser]:
    """Exchange a refresh token for a new pair, invalidating the old one."""
    result = await session.execute(
        sa.select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_secret(refresh_secret)
        )
    )
    token = result.scalar_one_or_none()
    if token is None:
        raise AuthError("unknown refresh token")

    if token.revoked_at is not None:
        # A revoked token being presented means the secret leaked, or a client
        # replayed one. Either way, end every session this user holds.
        await revoke_all_for_user(session, token.user_id)
        raise AuthError("refresh token has already been used")

    if token.expires_at <= datetime.now(UTC):
        raise AuthError("refresh token has expired")

    user = await session.get(AppUser, token.user_id)
    if user is None or not user.is_active:
        raise AuthError("account is disabled")

    access_token, secret = await issue_session(session, user, replaces=token)
    return access_token, secret, user


async def revoke_session(session: AsyncSession, refresh_secret: str) -> None:
    result = await session.execute(
        sa.select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_secret(refresh_secret)
        )
    )
    token = result.scalar_one_or_none()
    if token is not None and token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        sa.update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
