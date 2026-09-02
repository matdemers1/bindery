"""Session lifecycle: login, refresh rotation, logout.

Rotation revokes rather than deletes (invariant 3), which also gives us the
chain needed to detect a replayed refresh token.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import kdf
from api.auth.passwords import decoy_hash, verify_password
from api.auth.tokens import hash_refresh_secret, issue_access_token, new_refresh_secret
from api.db.models import AppUser, RefreshToken


class AuthError(Exception):
    """Authentication failed. The message is safe to show to the caller."""


async def _verify(password_hash: str, password: str) -> bool:
    """`verify_password`, off the event loop and behind the bounded pool.

    Argon2 at the default parameters is 64 MiB and tens of milliseconds of
    blocking C. Called inline from an `async def` handler it stopped every other
    request in the process, and two hundred simultaneous POSTs to a login form
    that has faced the open internet since ADR-008 were twelve gigabytes of
    transient allocation on a sixteen-gigabyte host. `api/auth/kdf.py` explains
    why the pool is small and fixed rather than `asyncio.to_thread`'s thirty-two.

    The hasher is looked up as a module global rather than captured, so a test
    that patches `api.auth.service.verify_password` still sees both calls — the
    decoy path included, which is the one that keeps the timings equal.
    """
    return await kdf.derive(verify_password, password_hash, password)


async def authenticate(session: AsyncSession, email: str, password: str) -> AppUser:
    result = await session.execute(
        sa.select(AppUser).where(AppUser.email == email.strip().lower())
    )
    user = result.scalar_one_or_none()

    # Verify against a decoy when the address does not exist, so both branches
    # do the same Argon2 work. Argon2 is deliberately slow — that is its job —
    # which made the short-circuit a clean timing oracle: a wrong password took
    # tens of milliseconds and an unknown address took none. The message was
    # already identical (REQ-135); the clock was not. Both branches go through
    # `_verify`, so both are also off the event loop.
    if user is None:
        await _verify(decoy_hash(), password)
        raise AuthError("invalid credentials")
    if not await _verify(user.password_hash, password):
        raise AuthError("invalid credentials")
    if not user.is_active:
        raise AuthError("account is disabled")
    return user


async def issue_session(
    session: AsyncSession, user: AppUser, *, replaces: RefreshToken | None = None
) -> tuple[str, str]:
    """Mint an access token and a fresh refresh token. Returns (access, refresh).

    The refresh row is created *first*, because the access token carries its id
    as `sid` — that is what makes the access token revocable rather than a
    thirty-minute bearer credential nobody can withdraw.
    """
    secret, secret_hash, expires_at = new_refresh_secret()

    refresh = RefreshToken(user_id=user.id, token_hash=secret_hash, expires_at=expires_at)
    session.add(refresh)
    await session.flush()

    access_token, _ = issue_access_token(user.id, refresh.id)

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


async def revoke_session_by_id(session: AsyncSession, session_id: uuid.UUID) -> bool:
    """Revoke one session by its row id — the `sid` an access token carries.

    Lets sign-out end the session server-side even when the refresh cookie did
    not arrive, which is the case that made "sign out" on a borrowed machine
    mean nothing but "forget my copy of the cookie".
    """
    token = await session.get(RefreshToken, session_id)
    if token is None or token.revoked_at is not None:
        return False
    token.revoked_at = datetime.now(UTC)
    return True


async def session_is_live(session: AsyncSession, session_id: uuid.UUID) -> bool:
    """Whether the session an access token names still exists and is not revoked.

    One indexed primary-key lookup on a request that already loads the user.
    Without it, `logout`, `change_password`, `redeem_reset_code` and `suspend`
    all revoke refresh tokens that the access token never consults, and the
    access token outlives every one of them by up to its full lifetime.
    """
    token = await session.get(RefreshToken, session_id)
    return token is not None and token.revoked_at is None


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        sa.update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
