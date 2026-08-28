"""Access-token minting and refresh-token secret generation."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from api.config import get_settings


class TokenError(Exception):
    """Raised when a token is absent, malformed, expired, or of the wrong type."""


def _now() -> datetime:
    return datetime.now(UTC)


def issue_access_token(user_id: uuid.UUID) -> tuple[str, datetime]:
    settings = get_settings()
    expires_at = _now() + timedelta(minutes=settings.jwt_access_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "typ": "access",
        "iat": int(_now().timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(8),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str) -> uuid.UUID:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("typ") != "access":
        raise TokenError("not an access token")
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed subject") from exc


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
