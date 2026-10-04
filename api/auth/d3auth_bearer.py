"""D3 Auth access tokens as Bearer credentials (BND-T-22.3, the D3 App contract).

D3 Constellation signs a person in to D3 Auth once and asks it for a token whose audience is this
Bindery (RFC 8707). Such a token is a JWT signed by the provider: it is checked against the
provider's published keys, for this issuer, for this audience, and for time — then mapped to an
account through the `(issuer, subject)` link and nothing else (never email, as everywhere in
`api/oidc.py`). An identity with no link answers `identity_not_linked`, and the app runs the link
flow (`POST /api/auth/native/link`), which proves the local account once with its own password.

The resource Bindery answers to is its own origin as the client reached it — the same URL the
manifest names in `signIn.d3auth.resource` — so nothing new needs configuring.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from api import oidc
from api.db.models import AppUser

log = logging.getLogger("bindery.d3auth_bearer")

# A minute either way, as the contract allows; more would widen every replay window.
LEEWAY_SECONDS = 60
# The provider's keys change rarely and a rotation publishes the new one before using it.
JWKS_TTL_SECONDS = 600
ALGORITHMS = ["RS256", "PS256", "ES256", "EdDSA"]


class NotD3AuthToken(Exception):
    """Not a token this module can speak for: no SSO configured, or not one of the provider's."""


class IdentityNotLinked(Exception):
    """A valid D3 Auth token for a person with no account linked here."""

    def __init__(self, issuer: str, subject: str) -> None:
        super().__init__("identity not linked")
        self.issuer = issuer
        self.subject = subject


@dataclass(frozen=True)
class Verified:
    issuer: str
    subject: str
    preferred_username: str | None
    claims: dict[str, Any]


_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _fetch_jwks(issuer: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5) as client:
        discovery = (await client.get(f"{issuer}/.well-known/openid-configuration")).json()
        return (await client.get(discovery["jwks_uri"])).json()


# Replaced in tests with a function answering a local key set.
JwksFetcher = Callable[[str], Any]
fetch_jwks: JwksFetcher = _fetch_jwks


async def _jwks(issuer: str, *, refresh: bool = False) -> dict[str, Any]:
    cached = _jwks_cache.get(issuer)
    if cached and not refresh and time.monotonic() - cached[0] < JWKS_TTL_SECONDS:
        return cached[1]
    keys = await fetch_jwks(issuer)
    _jwks_cache[issuer] = (time.monotonic(), keys)
    return keys


def _key_for(jwks: dict[str, Any], kid: str | None) -> jwt.PyJWK | None:
    for key in jwks.get("keys", []):
        if kid is None or key.get("kid") == kid:
            return jwt.PyJWK(key)
    return None


def looks_like_provider_token(token: str) -> bool:
    """A JWT with an asymmetric algorithm — Bindery's own access tokens are HS256."""
    try:
        return jwt.get_unverified_header(token).get("alg") in ALGORITHMS
    except jwt.PyJWTError:
        return False


async def verify(session: AsyncSession, token: str, *, resource: str) -> Verified:
    """Check a D3 Auth token for this Bindery. Raises NotD3AuthToken when it is not one."""
    config = await oidc.config(session)
    if not config.enabled or not looks_like_provider_token(token):
        raise NotD3AuthToken("SSO is off, or this is not a provider token")
    kid = jwt.get_unverified_header(token).get("kid")
    try:
        key = _key_for(await _jwks(config.issuer), kid) or _key_for(
            await _jwks(config.issuer, refresh=True), kid
        )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("could not read %s's keys: %s", config.issuer, exc)
        raise NotD3AuthToken("the provider's keys are unavailable") from exc
    if key is None:
        raise NotD3AuthToken("no key for this token")
    try:
        claims = await asyncio.to_thread(
            jwt.decode,
            token,
            key.key,
            algorithms=ALGORITHMS,
            audience=resource,
            issuer=config.issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise NotD3AuthToken(str(exc)) from exc
    return Verified(
        issuer=config.issuer,
        subject=str(claims["sub"]),
        preferred_username=claims.get("preferred_username"),
        claims=claims,
    )


async def user_for(session: AsyncSession, token: str, *, resource: str) -> AppUser:
    """The linked account a D3 Auth token speaks for. Raises IdentityNotLinked or NotD3AuthToken."""
    verified = await verify(session, token, resource=resource)
    identity = await oidc.identity_for(session, issuer=verified.issuer, subject=verified.subject)
    if identity is None:
        raise IdentityNotLinked(verified.issuer, verified.subject)
    user = await session.get(AppUser, identity.user_id)
    if user is None or not user.is_active:
        raise NotD3AuthToken("the linked account is disabled")
    return user
