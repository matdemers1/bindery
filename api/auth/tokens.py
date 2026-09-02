"""Access-token minting and refresh-token secret generation.

An access token names the **session** it was minted for, not just the user
(`sid`). Without that claim a signed JWT is a bearer credential that nothing can
withdraw for its full thirty minutes, and the scenarios the controls around it
exist for are exactly the ones it fails: a person who resets their password
because they think somebody has their session keeps that somebody signed in; a
sign-out on a borrowed laptop deletes the browser's copy and ends nothing
server-side. `sid` is the refresh token's row id, so revoking that row — which
logout, a password change, a reset, a suspension and reuse detection all already
do — now ends the access token with it.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from api.config import get_settings


class TokenError(Exception):
    """Raised when a token is absent, malformed, expired, or of the wrong type."""


@dataclass(frozen=True)
class AccessClaims:
    """Who the token is for, and which session issued it."""

    user_id: uuid.UUID
    session_id: uuid.UUID


def _now() -> datetime:
    return datetime.now(UTC)


def issue_access_token(user_id: uuid.UUID, session_id: uuid.UUID) -> tuple[str, datetime]:
    settings = get_settings()
    expires_at = _now() + timedelta(minutes=settings.jwt_access_ttl_minutes)
    payload = {
        "sub": str(user_id),
        # The refresh token row this session is. Checked against the database on
        # every request, so revoking the row revokes this token too.
        "sid": str(session_id),
        "typ": "access",
        "iat": int(_now().timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(8),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_claims(token: str) -> AccessClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("typ") != "access":
        raise TokenError("not an access token")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed subject") from exc
    try:
        # Required, not optional. A token without it is one this build did not
        # mint, and accepting it would restore the unrevokable token it exists
        # to remove.
        session_id = uuid.UUID(payload["sid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("access token names no session") from exc
    return AccessClaims(user_id=user_id, session_id=session_id)


def decode_access_token(token: str) -> uuid.UUID:
    """Just the subject, for callers that have no database session to hand.

    Signature and expiry only — it says nothing about whether the session behind
    the token is still live. `api/auth/dependencies.py` uses
    `decode_access_claims` and checks that; anything using this one is trusting
    a token for up to `jwt_access_ttl_minutes` after a revocation.
    """
    return decode_access_claims(token).user_id


def new_refresh_secret() -> tuple[str, str, datetime]:
    """Return (secret, secret_hash, expires_at).

    The secret goes to the client; only the hash is stored, so a database read
    does not yield usable tokens.
    """
    settings = get_settings()
    secret = secrets.token_urlsafe(48)
    expires_at = _now() + timedelta(days=settings.jwt_refresh_ttl_days)
    return secret, hash_refresh_secret(secret), expires_at


def hash_refresh_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()
