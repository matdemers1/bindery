"""Sign in with D3 Auth: the rules, apart from the routes that apply them (Phase 20).

Bindery is the reference relying party for D3 Auth, and the shape of that is set by one
sentence: **adding a second way in must not weaken the first**. Everything here follows from
it.

- A sign-in through the provider ends in an ordinary Bindery session, minted by
  `api.auth.service.issue_session` exactly as a password login mints one. Nothing downstream of
  `current_user` learns that SSO exists, which is what keeps the rest of the application, and
  its permission suite, unchanged.
- An account is found by `(issuer, subject)` and by nothing else. Not by email: an address is a
  display value that changes, is reused, and at some providers is chosen by the person claiming
  it. Matching on it is account takeover wearing the costume of convenience.
- A first sign-in provisions an account **only** when the provider says the person holds a role
  in Bindery. D3 Auth is deny-by-default, so a role is an administrator's decision that already
  happened; no role means no account, not an empty one.
- `is_admin` is never granted here. Bindery requires two-factor of its administrators (REQ-156)
  and that rule does not move because the identity arrived from somewhere else; the mapping
  raises an account to administrator only once Bindery's own TOTP is enrolled.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from api import settings_store
from api.audit import record
from api.config import get_settings
from api.db.enums import ActorType, LibraryKind, MembershipRole
from api.db.models import AppUser, Library, Membership, OidcIdentity
from api.db.models.user import RefreshToken

log = logging.getLogger("bindery.oidc")

SsoMode = Literal["off", "optional", "required"]

#: What the manifest declares, highest first — the same order D3 Auth returns them in.
ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"
GUEST_ROLE = "guest"
KNOWN_ROLES = (ADMIN_ROLE, MEMBER_ROLE, GUEST_ROLE)

#: A guest is somebody else's visitor; 5 GB is enough to be useful and small enough that nobody
#: fills the host by accident. A member gets what a local account gets: no limit until an
#: administrator sets one.
GUEST_QUOTA_BYTES = 5 * 1024 * 1024 * 1024

#: How long a half-finished sign-in stays valid. Long enough to type a password and a code,
#: short enough that a cookie left on a shared machine is worthless.
TRANSACTION_TTL_SECONDS = 600


class SsoDisabled(Exception):
    """SSO is off, or not configured. The routes answer 404 rather than explaining."""


class SsoUnavailable(Exception):
    """Configured, but the provider cannot be reached right now."""


class SignInRefused(Exception):
    """The sign-in itself was refused, with a reason fit to show a person."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class OidcConfig:
    """What the operator configured. Absent issuer, client id or secret means off."""

    issuer: str
    client_id: str
    client_secret: str
    mode: SsoMode

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and bool(self.issuer and self.client_id and self.client_secret)


async def config(session: AsyncSession) -> OidcConfig:
    """Read the configuration: Settings first, environment as the fallback."""
    mode = (await settings_store.get(session, settings_store.SSO_MODE) or "off").strip().lower()
    if mode not in ("off", "optional", "required"):
        log.warning("SSO_MODE is %r, which is not off, optional or required — treating it as off", mode)
        mode = "off"
    return OidcConfig(
        issuer=(await settings_store.get(session, settings_store.OIDC_ISSUER) or "").strip().rstrip("/"),
        client_id=(await settings_store.get(session, settings_store.OIDC_CLIENT_ID) or "").strip(),
        client_secret=(await settings_store.get(session, settings_store.OIDC_CLIENT_SECRET) or "").strip(),
        mode=mode,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# The transaction cookie (finding F-12)
# ---------------------------------------------------------------------------
#
# The PKCE verifier, the state and the nonce belong to the browser that began the sign-in. Held
# in a table keyed on `state`, they belong to whoever supplies the state — which is the
# attacker. D3 Auth's own reference app had that bug: it allowed login-CSRF and, through
# account linking, role theft.
#
# So they travel in a short-lived HttpOnly cookie, signed with the same secret as everything
# else Bindery signs. Signed rather than merely opaque because the callback must be able to say
# "this is the transaction *I* issued" without a server-side lookup to be raced.


def _signing_key() -> bytes:
    return hashlib.sha256(f"oidc-transaction:{get_settings().jwt_secret}".encode()).digest()


def seal_transaction(payload: dict[str, Any]) -> str:
    """A transaction, signed and safe to hand to a browser."""
    body = base64.urlsafe_b64encode(json.dumps({**payload, "iat": int(time.time())}).encode()).decode().rstrip("=")
    signature = hashlib.blake2b(body.encode(), key=_signing_key(), digest_size=32).hexdigest()
    return f"{body}.{signature}"


def open_transaction(sealed: str | None) -> dict[str, Any]:
    """The payload, if this is one of ours and still fresh. Raises otherwise."""
    if not sealed or "." not in sealed:
        raise SignInRefused("that sign-in has expired — start again")
    body, _, signature = sealed.partition(".")
    expected = hashlib.blake2b(body.encode(), key=_signing_key(), digest_size=32).hexdigest()
    if not secrets.compare_digest(signature, expected):
        raise SignInRefused("that sign-in could not be verified — start again")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if time.time() - float(payload.get("iat", 0)) > TRANSACTION_TTL_SECONDS:
        raise SignInRefused("that sign-in took too long — start again")
    return payload


# ---------------------------------------------------------------------------
# The provider's refresh token, kept so roles can be re-read (REQ-207)
# ---------------------------------------------------------------------------


def _cipher() -> Fernet:
    digest = hashlib.sha256(get_settings().jwt_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(token: str | None) -> str | None:
    return _cipher().encrypt(token.encode()).decode() if token else None


def decrypt_token(sealed: str | None) -> str | None:
    if not sealed:
        return None
    try:
        return _cipher().decrypt(sealed.encode()).decode()
    except InvalidToken:
        log.error("cannot decrypt a stored provider refresh token — JWT_SECRET has changed")
        return None


# ---------------------------------------------------------------------------
# Identities, provisioning and roles
# ---------------------------------------------------------------------------


async def identity_for(session: AsyncSession, *, issuer: str, subject: str) -> OidcIdentity | None:
    """The link for this provider identity, if an account has one."""
    return (
        await session.execute(
            sa.select(OidcIdentity).where(
                OidcIdentity.issuer == issuer,
                OidcIdentity.subject == subject,
                OidcIdentity.unlinked_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def identity_of(session: AsyncSession, *, user: AppUser) -> OidcIdentity | None:
    """The link this account holds, if it holds one."""
    return (
        await session.execute(
            sa.select(OidcIdentity).where(
                OidcIdentity.user_id == user.id, OidcIdentity.unlinked_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def link(
    session: AsyncSession,
    *,
    user: AppUser,
    issuer: str,
    subject: str,
    preferred_username: str | None,
    refresh_token: str | None,
    origin: str,
) -> OidcIdentity:
    """Attach a provider identity to an account. Refuses if either half is already spoken for."""
    if (await identity_for(session, issuer=issuer, subject=subject)) is not None:
        raise SignInRefused("that D3 Auth account is already connected to somebody here")
    if (await identity_of(session, user=user)) is not None:
        raise SignInRefused("this account is already connected to D3 Auth")

    identity = OidcIdentity(
        user_id=user.id,
        issuer=issuer,
        subject=subject,
        preferred_username=preferred_username,
        refresh_token_enc=encrypt_token(refresh_token),
        origin=origin,
    )
    session.add(identity)
    await session.flush()
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="oidc_linked",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"issuer": issuer, "subject": subject, "origin": origin},
    )
    log.info("account %s linked to %s", user.email, issuer)
    return identity


async def disconnect(session: AsyncSession, *, user: AppUser) -> bool:
    """Tombstone the link. The caller has already proved the local password.

    Named `disconnect` rather than `unlink` deliberately: `unlink` is how a file is deleted in
    Python, and `tests/test_no_destructive_paths.py` reads call sites by name — a database
    operation borrowing a filesystem verb makes that guard cry wolf, and a guard that cries wolf
    is one somebody switches off.
    """
    identity = await identity_of(session, user=user)
    if identity is None:
        return False
    before = {"issuer": identity.issuer, "subject": identity.subject}
    # Tombstoned, not deleted (REQ-090). "Is this account connected" and "was it ever" are two
    # questions, and the second is the one an audit asks after somebody loses access.
    identity.unlinked_at = datetime.now(UTC)
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="oidc_unlinked",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        before=before,
    )
    log.info("account %s unlinked from %s", user.email, before["issuer"])
    return True


def quota_for(roles: list[str]) -> int | None:
    """What a newly provisioned account may hold, by the highest role it arrived with."""
    return None if ADMIN_ROLE in roles or MEMBER_ROLE in roles else GUEST_QUOTA_BYTES


async def provision(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str,
    display_name: str | None,
    preferred_username: str | None,
    roles: list[str],
    refresh_token: str | None,
) -> AppUser:
    """Create the account a first SSO sign-in earns, with its library and quota (REQ-205).

    Deliberately the same shape an invitation produces: one account, one personal library, owner
    of it and of nothing else. What differs is only who decided — an administrator at the
    provider rather than an administrator here.
    """
    if not roles:
        raise SignInRefused("this account has no access to Bindery yet — ask whoever runs it")

    email = (email or "").strip().lower()
    if not email:
        raise SignInRefused("the provider did not say who you are")
    if (
        await session.execute(sa.select(AppUser).where(AppUser.email == email))
    ).scalar_one_or_none() is not None:
        # An address that already exists here is exactly the case auto-linking would get wrong.
        raise SignInRefused(
            "an account with that address already exists here. Sign in with your password, "
            "then connect D3 Auth from Settings"
        )

    user = AppUser(
        email=email,
        # Never a usable password: this account signs in through the provider. A local password
        # is set the way any other account sets one, through a reset an administrator issues.
        password_hash=f"sso-only:{secrets.token_urlsafe(32)}",
        display_name=(display_name or "").strip() or None,
        storage_quota_bytes=quota_for(roles),
    )
    library = Library(name=display_name or email.split("@")[0] or "Personal", kind=LibraryKind.PERSONAL)
    session.add_all([user, library])
    await session.flush()
    session.add(Membership(user_id=user.id, library_id=library.id, role=MembershipRole.OWNER))

    await link(
        session,
        user=user,
        issuer=issuer,
        subject=subject,
        preferred_username=preferred_username,
        refresh_token=refresh_token,
        origin="sso",
    )
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="account_created",
        actor_type=ActorType.SYSTEM,
        after={"via": "d3auth", "email": email, "roles": roles, "library": library.name},
    )
    log.info("provisioned %s from %s with roles %s", email, issuer, roles)
    return user


async def apply_roles(session: AsyncSession, *, user: AppUser, roles: list[str]) -> bool:
    """Map the provider's roles onto what Bindery has. Returns whether anything changed.

    Only `is_admin` moves: Bindery's other permissions are memberships in libraries, which
    belong to the household and are nobody else's to hand out.

    Administrator is granted only to an account that has Bindery's own two-factor enrolled
    (REQ-156). An administrator here can reset every other password, and that rule is not
    weakened by the identity having arrived from elsewhere. The claim is not lost — it applies
    the moment they enrol.
    """
    wants_admin = ADMIN_ROLE in roles
    if wants_admin and not user.totp_enabled:
        if user.is_admin:
            return False
        log.info(
            "not granting administrator to %s from a D3 Auth role: Bindery two-factor is not "
            "enrolled (REQ-156)", user.email,
        )
        return False

    if user.is_admin == wants_admin:
        return False

    before = {"is_admin": user.is_admin}
    user.is_admin = wants_admin
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="admin_granted" if wants_admin else "admin_revoked",
        actor_type=ActorType.SYSTEM,
        before=before,
        after={"is_admin": wants_admin, "via": "d3auth", "roles": roles},
    )
    log.warning("administrator %s for %s, from D3 Auth roles", "granted" if wants_admin else "revoked", user.email)
    return True


async def refresh_roles(session: AsyncSession, *, user: AppUser) -> list[str] | None:
    """Re-read this account's roles from the provider, and apply them (REQ-207).

    Called when Bindery renews its own session, which is the moment that costs nothing: the
    person is here, a round trip is affordable, and the answer is at most one renewal old. A
    grant withdrawn at the provider therefore lands on the next request rather than at the next
    sign-in, which for a session that renews itself might be never.

    Returns the roles, or None when there is nothing to ask about — no link, no stored token, or
    a provider that did not answer. A provider that is down must not sign anybody out: the
    session they already hold is Bindery's to honour.
    """
    identity = await identity_of(session, user=user)
    if identity is None:
        return None
    token = decrypt_token(identity.refresh_token_enc)
    if not token:
        return None

    configuration = await config(session)
    if not configuration.enabled or configuration.issuer != identity.issuer:
        return None

    try:
        from d3auth_client import D3AuthClient

        client = D3AuthClient(
            issuer=configuration.issuer,
            client_id=configuration.client_id,
            client_secret=configuration.client_secret,
            sso_mode=configuration.mode,
        )
        renewed = await client.refresh(token)
    except Exception as failure:  # noqa: BLE001 - any failure here means "ask again next time"
        log.info("could not re-read roles for %s: %s", user.email, failure)
        return None

    if renewed.refresh_token:
        identity.refresh_token_enc = encrypt_token(renewed.refresh_token)
    identity.last_seen_at = datetime.now(UTC)
    await apply_roles(session, user=user, roles=renewed.roles)
    return renewed.roles


async def end_sessions(
    session: AsyncSession, *, issuer: str, subject: str, sid: str | None
) -> int:
    """End the Bindery sessions a back-channel logout names (REQ-208).

    With a `sid`, only the sessions that sign-in produced: signing out on the phone must not
    sign the laptop out too. Without one, every session this identity holds — which is what the
    provider means when it omits it.
    """
    identity = await identity_for(session, issuer=issuer, subject=subject)
    if identity is None:
        return 0

    condition = [RefreshToken.user_id == identity.user_id, RefreshToken.revoked_at.is_(None)]
    if sid:
        condition.append(RefreshToken.oidc_sid == sid)

    result = await session.execute(
        sa.update(RefreshToken).where(sa.and_(*condition)).values(revoked_at=sa.func.now())
    )
    ended = result.rowcount or 0
    if ended:
        await record(
            session,
            entity_type="app_user",
            entity_id=identity.user_id,
            action="logout",
            actor_type=ActorType.SYSTEM,
            after={"via": "d3auth_backchannel", "sessions_ended": ended, "sid": sid},
        )
    log.info("back-channel logout ended %d session(s) for %s", ended, identity.user_id)
    return ended
