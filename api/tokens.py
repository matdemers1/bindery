"""Scoped API tokens (T-8.5, REQ-107).

The scanner needs to POST a scan without a browser login, and a script that
exports nightly should not carry a password. So: a bearer token with a name, a
set of scopes, and a set of libraries.

Three rules make this safe enough to hand to a shell script:

**A token can never exceed the person who made it.** Its libraries are
intersected with the creator's current memberships on *every* request, not just
at creation. Removing someone from a library removes their tokens' reach at the
same moment, without anyone having to hunt down the tokens.

**Scopes are a closed set.** An unrecognised scope is refused at creation rather
than ignored, because a scope that silently does nothing is a permission you
think you granted.

**The secret is shown once.** Only a SHA-256 of the token is stored. It is 256
bits of randomness, so there is nothing to salt and nothing to brute-force, and
a stolen backup is not a set of working credentials.
"""

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import ApiToken, Membership

log = logging.getLogger("bindery.tokens")

PREFIX = "bnd_"

# The closed set. Deliberately coarse — a token that can do half of one screen
# is a token nobody can reason about.
SCOPES = {
    "read": "Search and read documents.",
    "upload": "Add new files.",
    "export": "Run exports and backups.",
    "admin": "Everything the creating user can do.",
}

# Scopes that a scope implies. `admin` is the only one that widens.
IMPLIES = {"admin": frozenset(SCOPES) - {"admin"}}


@dataclass
class IssuedToken:
    """The one and only time the secret exists outside the caller's hands."""

    record: ApiToken
    secret: str


@dataclass
class TokenIdentity:
    """A verified token, reduced to what it may currently do."""

    token: ApiToken
    user_id: uuid.UUID
    scopes: frozenset[str]
    library_ids: tuple[uuid.UUID, ...]

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


def hash_token(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def expand(scopes: list[str]) -> frozenset[str]:
    granted = set(scopes)
    for scope in list(granted):
        granted |= IMPLIES.get(scope, frozenset())
    return frozenset(granted)


async def issue(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str,
    scopes: list[str],
    library_ids: list[uuid.UUID],
    expires_in_days: int | None = None,
) -> IssuedToken:
    unknown = sorted(set(scopes) - set(SCOPES))
    if unknown:
        # Refusing beats ignoring: a scope that silently does nothing is a
        # permission the caller believes they granted.
        raise ValueError(f"unknown scope(s): {', '.join(unknown)}")
    if not scopes:
        raise ValueError("a token with no scopes can do nothing; give it at least one")

    allowed = set(
        (
            await session.execute(
                sa.select(Membership.library_id).where(Membership.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    requested = set(library_ids) or allowed
    beyond = requested - allowed
    if beyond:
        raise ValueError("a token cannot reach a library its creator cannot")

    secret = PREFIX + secrets.token_urlsafe(32)
    record = ApiToken(
        user_id=user_id,
        name=name.strip() or "unnamed token",
        token_hash=hash_token(secret),
        prefix=secret[: len(PREFIX) + 6],
        scopes=sorted(scopes),
        library_ids=sorted(requested),
        expires_at=(
            datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
        ),
    )
    session.add(record)
    await session.flush()
    log.info("issued token %s (%s) for user %s", record.prefix, record.scopes, user_id)
    return IssuedToken(record=record, secret=secret)


async def verify(session: AsyncSession, secret: str) -> TokenIdentity | None:
    """Resolve a bearer token to what it may do *right now*."""
    if not secret.startswith(PREFIX):
        return None

    record = (
        await session.execute(
            sa.select(ApiToken).where(ApiToken.token_hash == hash_token(secret))
        )
    ).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return None
    if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
        return None

    # Re-intersected on every request, not trusted from creation time. This is
    # what makes removing a membership immediately effective.
    current = set(
        (
            await session.execute(
                sa.select(Membership.library_id).where(Membership.user_id == record.user_id)
            )
        )
        .scalars()
        .all()
    )
    reachable = tuple(sorted(set(record.library_ids) & current))
    if not reachable:
        log.warning("token %s has no reachable libraries; refusing", record.prefix)
        return None

    record.last_used_at = sa.func.now()
    return TokenIdentity(
        token=record,
        user_id=record.user_id,
        scopes=expand(record.scopes),
        library_ids=reachable,
    )


async def revoke(session: AsyncSession, token_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Revoked, never deleted — the record of what existed is the audit trail."""
    record = (
        await session.execute(
            sa.select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user_id)
        )
    ).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = sa.func.now()
    log.info("revoked token %s", record.prefix)
    return True
