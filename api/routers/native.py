"""The D3 App contract for native clients (BND-P-22, CON-ADR-003): the manifest, and native
sessions in JSON.

D3 Constellation signs in here without a browser. Everything a native session is rides on what a
browser session already is — the same `refresh_token` rows, the same rotation with reuse detection,
the same throttle and the same audit — so the permission suite keeps meaning what it means: nothing
downstream of `current_user` can tell a phone from a browser. What differs is the envelope: tokens
come back in JSON rather than cookies, the second factor is a separate request carrying a
short-lived signed challenge, and every refusal is problem+json with a registered type.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api import account_deletion, accounts, events, oidc
from api import tokens as api_tokens
from api.audit import record
from api.auth import d3auth_bearer, service, throttle
from api.auth import totp as totp_codes
from api.auth.client import client_ip
from api.auth.passwords import WeakPassword, validate_password
from api.auth.tokens import TokenError, access_ttl_minutes, decode_access_claims
from api.config import get_settings
from api.db.enums import ActorType
from api.db.models import AppUser, RefreshToken
from api.db.session import get_session
from api.problems import problem
from api.routers.auth import retire_after_recovery
from api.version import build_of_this_process

router = APIRouter(prefix="/auth/native", tags=["native"])
well_known = APIRouter(tags=["native"])

# How long the step between password and code may take. Long enough to open an authenticator,
# short enough that a challenge lifted from a log is worthless.
CHALLENGE_TTL = timedelta(minutes=5)
CHALLENGE_TYPE = "native-challenge"
# Accepting an invite (BND-T-23.2): the account exists after the first step, and this finishes its
# second factor. The contract asks for at least ten minutes — setting up an authenticator for the
# first time takes longer than reading a code off one.
ENROL_TTL = timedelta(minutes=15)
ENROL_TYPE = "native-enrol"

# What D3 Constellation may show for this server; a client shows a feature only when it is listed.
CAPABILITIES = (
    "bindery.search",
    "bindery.viewer",
    "bindery.upload",
    "bindery.review",
    "bindery.ask",
    "bindery.vault",
)


class Device(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(min_length=1, max_length=40)


class SignIn(BaseModel):
    """Either the password step or the code step: the contract keeps them on one endpoint."""

    email: str | None = None
    password: str | None = None
    device: Device | None = None
    challenge: str | None = None
    totp: str | None = None
    recoveryCode: str | None = None


class Link(BaseModel):
    email: str
    password: str
    totp: str | None = None
    recoveryCode: str | None = None


class Refresh(BaseModel):
    refreshToken: str


class InviteAccept(BaseModel):
    """Either the account step or the second-factor step, on one endpoint like sign-in."""

    token: str | None = Field(default=None, max_length=200)
    displayName: str | None = Field(default=None, max_length=120)
    password: str | None = None
    device: Device | None = None
    challenge: str | None = None
    enrolTotp: str | None = Field(default=None, max_length=16)


class DeleteAccount(BaseModel):
    confirmation: str = Field(max_length=253)
    totp: str = Field(min_length=1, max_length=16)


def _base(request: Request) -> str:
    """The server's own origin, as the client reached it (uvicorn trusts the proxy's
    X-Forwarded-Proto, so this is https behind the tunnel)."""
    return str(request.base_url).rstrip("/")


@well_known.get("/.well-known/d3-app.json", include_in_schema=False)
async def manifest(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """The D3 App manifest (BND-T-22.1): what this server is, what it offers, where to sign in."""
    base = _base(request)
    build = build_of_this_process()
    sso = await oidc.config(session)
    body: dict[str, Any] = {
        "product": "bindery",
        "name": "Bindery",
        "version": build.ref if build.ref != "unknown" else "0.1.0",
        "revision": build.short if build.commit != "unknown" else None,
        "contract": 1,
        "capabilities": list(CAPABILITIES),
        "signIn": {"methods": ["password", "totp", "recovery_code"]},
        "endpoints": {
            "nativeSignIn": f"{base}/api/auth/native/signin",
            "nativeRefresh": f"{base}/api/auth/native/refresh",
            "nativeRevoke": f"{base}/api/auth/native/revoke",
            "me": f"{base}/api/auth/native/me",
            "link": f"{base}/api/auth/native/link" if sso.enabled else None,
            "inviteAccept": f"{base}/api/auth/native/invite",
            "deleteAccount": f"{base}/api/auth/native/delete-account",
            "relayRegister": f"{base}/api/push/native/register",
        },
    }
    if sso.enabled:
        # D3 Auth tokens for this Bindery are accepted once the operator has connected a provider
        # (BND-T-22.3): the app asks the issuer for this origin as the audience.
        body["signIn"]["methods"].append("d3auth")
        body["signIn"]["d3auth"] = {"issuer": sso.issuer, "resource": base}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _challenge_for(
    user: AppUser,
    device: Device | None,
    *,
    kind: str = CHALLENGE_TYPE,
    ttl: timedelta = CHALLENGE_TTL,
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "typ": kind,
        "sub": str(user.id),
        "dev": [device.name, device.platform] if device else None,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _read_challenge(
    token: str, *, kind: str = CHALLENGE_TYPE
) -> tuple[uuid.UUID, tuple[str, str] | None]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    # A sign-in challenge cannot finish an enrolment, nor the other way round: the type is part
    # of what was signed.
    if payload.get("typ") != kind:
        raise TokenError("not a challenge of this kind")
    device = payload.get("dev")
    return uuid.UUID(payload["sub"]), (device[0], device[1]) if device else None


async def _session_body(
    session: AsyncSession, user: AppUser, device: tuple[str, str] | None
) -> dict[str, Any]:
    access_token, refresh_secret = await service.issue_session(session, user, device=device)
    claims = decode_access_claims(access_token)
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="login",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"via": "native", "device": device[0] if device else None},
    )
    return {
        "accessToken": access_token,
        "refreshToken": refresh_secret,
        "expiresIn": access_ttl_minutes(native=True) * 60,
        "session": {"id": str(claims.session_id)},
    }


def _throttled(limited: throttle.Throttled) -> JSONResponse:
    return problem(
        429,
        "throttled",
        "Too many attempts",
        detail=f"Try again in {limited.retry_after} seconds.",
        headers={"Retry-After": str(limited.retry_after)},
        retryAfter=limited.retry_after,
    )


@router.post("/signin")
async def sign_in(
    payload: SignIn, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Password, then — for an account with an authenticator — a code, then tokens.

    The throttle is the web login's throttle: same table, same windows, and the code step counts
    against the same account, or the code would be the unthrottled half of the sign-in.
    """
    ip = client_ip(request)
    if payload.challenge:
        return await _second_factor(payload, ip, session)
    if not payload.email or payload.password is None:
        return problem(400, None, "Email and password are required")
    try:
        await throttle.check(session, payload.email, ip)
    except throttle.Throttled as limited:
        await session.commit()
        return _throttled(limited)
    try:
        user = await service.authenticate(session, payload.email, payload.password)
    except service.AuthError:
        await throttle.record(session, payload.email, ip, succeeded=False)
        await session.commit()
        # One answer for every failure, as on the web (REQ-135).
        return problem(401, "invalid_credentials", "Email or password is wrong")
    device = (payload.device.name, payload.device.platform) if payload.device else None
    if user.totp_enabled:
        await session.commit()
        return JSONResponse(
            {"next": "totp", "challenge": _challenge_for(user, payload.device)}, status_code=202
        )
    await throttle.record(session, payload.email, ip, succeeded=True)
    body = await _session_body(session, user, device)
    await session.commit()
    return JSONResponse(body)


async def _second_factor(payload: SignIn, ip: str | None, session: AsyncSession) -> Response:
    try:
        user_id, device = _read_challenge(payload.challenge or "")
    except TokenError:
        return problem(
            401,
            "invalid_code",
            "This sign-in has expired",
            detail="Start again with your email and password.",
        )
    user = await session.get(AppUser, user_id)
    if user is None or not user.is_active:
        return problem(401, "invalid_credentials", "Email or password is wrong")
    try:
        await throttle.check(session, user.email, ip)
    except throttle.Throttled as limited:
        await session.commit()
        return _throttled(limited)
    try:
        factor = await accounts.check_second_factor(
            session, user=user, code=(payload.totp or payload.recoveryCode or "").strip()
        )
    except accounts.AccountError:
        await throttle.record(session, user.email, ip, succeeded=False)
        await session.commit()
        return problem(401, "invalid_code", "That code didn't work")
    await throttle.record(session, user.email, ip, succeeded=True)
    if factor is accounts.SecondFactor.RECOVERY:
        await retire_after_recovery(session, user)
    body = await _session_body(session, user, device)
    await session.commit()
    return JSONResponse(body)


@router.post("/refresh")
async def refresh(payload: Refresh, session: AsyncSession = Depends(get_session)) -> Response:
    """Rotate. A rotated token presented again ends every session the user holds."""
    try:
        access_token, refresh_secret, user = await service.rotate_session(
            session, payload.refreshToken
        )
    except service.RefreshReused:
        await session.commit()
        return problem(401, "refresh_reused", "This sign-in was used twice and has been ended")
    except service.AuthError:
        await session.commit()
        return problem(401, "session_revoked", "This sign-in has ended")
    await oidc.refresh_roles(session, user=user)
    await session.commit()
    return JSONResponse(
        {
            "accessToken": access_token,
            "refreshToken": refresh_secret,
            "expiresIn": access_ttl_minutes(native=True) * 60,
        }
    )


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value or None if scheme.lower() == "bearer" else None


class _NotLinked(Exception):
    pass


async def _live_user(
    request: Request, session: AsyncSession
) -> tuple[AppUser, uuid.UUID | None] | None:
    """The account a Bearer token speaks for, and its Bindery session (none for a D3 Auth token).
    Raises `_NotLinked` for a valid D3 Auth token with no account linked here."""
    token = _bearer(request)
    if not token:
        return None
    if d3auth_bearer.looks_like_provider_token(token):
        try:
            user = await d3auth_bearer.user_for(session, token, resource=_base(request))
        except d3auth_bearer.IdentityNotLinked as exc:
            raise _NotLinked from exc
        except d3auth_bearer.NotD3AuthToken:
            return None
        return user, None
    try:
        claims = decode_access_claims(token)
    except TokenError:
        return None
    if not await service.session_is_live(session, claims.session_id):
        return None
    account = await session.get(AppUser, claims.user_id)
    return (account, claims.session_id) if account is not None and account.is_active else None


@router.post("/revoke", status_code=204)
async def revoke(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """Sign out: the session the Bearer token names ends here, now."""
    try:
        live = await _live_user(request, session)
    except _NotLinked:
        live = None
    if live is None or live[1] is None:
        # A D3 Auth token has no session here to end; signing out of D3 Auth is D3 Auth's.
        return problem(401, "session_revoked", "This sign-in has ended")
    user, session_id = live[0], live[1]
    await service.revoke_session_by_id(session, session_id)
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="logout",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"via": "native"},
    )
    await session.commit()
    return Response(status_code=204)


@router.get("/me")
async def me(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """The account, as the contract's `me` describes it."""
    try:
        live = await _live_user(request, session)
    except _NotLinked:
        return problem(
            401,
            "identity_not_linked",
            "This D3 Auth account isn't linked here yet",
            detail="Link it once with your Bindery email, password and code.",
        )
    if live is None:
        return problem(401, "session_revoked", "Sign in again")
    user, _ = live
    return JSONResponse(
        {
            "accountId": str(user.id),
            "email": user.email,
            "displayName": user.display_name or user.email,
            "roles": ["admin"] if user.is_admin else ["member"],
        }
    )


@router.post("/link")
async def link(
    payload: Link, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Link the D3 Auth identity in the Bearer token to this Bindery account, once (BND-T-22.3).

    Proves the local account with its own password and second factor, throttled exactly like a
    sign-in — linking is a sign-in that leaves a lasting connection behind.
    """
    token = _bearer(request)
    if not token:
        return problem(401, "session_revoked", "Sign in to D3 Auth first")
    try:
        verified = await d3auth_bearer.verify(session, token, resource=_base(request))
    except d3auth_bearer.NotD3AuthToken:
        return problem(401, "session_revoked", "This D3 Auth sign-in isn't valid here")
    ip = client_ip(request)
    try:
        await throttle.check(session, payload.email, ip)
    except throttle.Throttled as limited:
        await session.commit()
        return _throttled(limited)
    try:
        user = await service.authenticate(session, payload.email, payload.password)
    except service.AuthError:
        await throttle.record(session, payload.email, ip, succeeded=False)
        await session.commit()
        return problem(401, "invalid_credentials", "Email or password is wrong")
    if user.totp_enabled:
        try:
            factor = await accounts.check_second_factor(
                session, user=user, code=(payload.totp or payload.recoveryCode or "").strip()
            )
        except accounts.AccountError:
            await throttle.record(session, payload.email, ip, succeeded=False)
            await session.commit()
            return problem(401, "invalid_code", "That code didn't work")
        if factor is accounts.SecondFactor.RECOVERY:
            await retire_after_recovery(session, user)
    await throttle.record(session, payload.email, ip, succeeded=True)
    try:
        await oidc.link(
            session,
            user=user,
            issuer=verified.issuer,
            subject=verified.subject,
            preferred_username=verified.preferred_username,
            refresh_token=None,
            origin="native-link",
        )
    except oidc.SignInRefused as refused:
        await session.rollback()
        return problem(409, None, "Already linked", detail=str(refused))
    await session.commit()
    return JSONResponse({"linked": True})


# ---------------------------------------------------------------------------------------------
# Phase II: accepting an invite, and deleting the account (BND-P-23)
# ---------------------------------------------------------------------------------------------


def _invite_invalid() -> JSONResponse:
    # One answer for unknown, used, withdrawn and expired: telling them apart says which guess was
    # real, and never `invalid_credentials`, which would hint that an account exists.
    return problem(
        410,
        "invite_invalid",
        "This invite can't be used",
        detail="It has been used, withdrawn or has expired. Ask for a new one.",
    )


@router.post("/invite")
async def accept_invite(
    payload: InviteAccept, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Accept an invitation from the app (BND-T-23.2): the account, then its authenticator.

    The invitation is spent by the same `accounts.accept` the join page uses, so the library, its
    name and the storage quota come from the invitation and the acceptance is audited exactly as
    on the web. What differs is that the app enrols the authenticator in the same breath and is
    handed a native session only once a code from it proves the enrolment took.
    """
    ip = client_ip(request)
    if payload.challenge:
        return await _finish_enrolment(payload, ip, session)
    if not payload.token or payload.password is None:
        return problem(400, None, "A token and a password are required")

    # The web join page's key, so a guesser cannot spread attempts across the two doors.
    key = f"invite:{payload.token[:12]}"
    try:
        await throttle.check(session, key, ip)
    except throttle.Throttled as limited:
        await session.commit()
        return _throttled(limited)
    try:
        invitation = await accounts.find_invitation(session, payload.token)
    except accounts.AccountError:
        await throttle.record(session, key, ip, succeeded=False)
        await session.commit()
        return _invite_invalid()
    try:
        validate_password(payload.password, email=invitation.email)
    except WeakPassword as weak:
        return problem(422, "weak_password", "Choose a stronger password", detail=str(weak))
    try:
        user = await accounts.accept(
            session,
            token=payload.token,
            password=payload.password,
            display_name=payload.displayName,
        )
    except accounts.AccountError:
        # Spent between the look and the write: the same answer as any other unusable token.
        await session.rollback()
        return _invite_invalid()
    await record(
        session, entity_type="app_user", entity_id=user.id, action="account_created",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        after={"via": "invitation", "client": "native",
               "device": payload.device.name if payload.device else None},
    )
    # The authenticator is started here and confirmed by the next step — never trusted before a
    # code from it has been verified, exactly as on Settings.
    secret = totp_codes.new_secret()
    user.totp_secret = secret
    user.totp_confirmed_at = None
    user.totp_last_step = None
    await throttle.record(session, key, ip, succeeded=True)
    await session.commit()
    return JSONResponse(
        {
            "challenge": _challenge_for(user, payload.device, kind=ENROL_TYPE, ttl=ENROL_TTL),
            "enrolment": {
                "secret": secret,
                "otpauthUri": totp_codes.provisioning_uri(secret, email=user.email),
                "digits": totp_codes.DIGITS,
                "period": totp_codes.STEP_SECONDS,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


async def _finish_enrolment(
    payload: InviteAccept, ip: str | None, session: AsyncSession
) -> Response:
    expired = problem(
        401,
        "invalid_code",
        "This setup has expired",
        detail="Your account exists: sign in with your email and password.",
    )
    try:
        user_id, device = _read_challenge(payload.challenge or "", kind=ENROL_TYPE)
    except TokenError:
        return expired
    user = await session.get(AppUser, user_id)
    # Already enrolled means this challenge has been used: it finishes one enrolment, once.
    if user is None or not user.is_active or user.totp_enabled:
        return expired
    try:
        await throttle.check(session, user.email, ip)
    except throttle.Throttled as limited:
        await session.commit()
        return _throttled(limited)
    try:
        codes = await accounts.confirm_totp(
            session, user=user, code=(payload.enrolTotp or "").strip()
        )
    except accounts.AccountError:
        # The challenge stays valid until it expires, as the contract has it; the throttle on the
        # account is what stops a wrong code being a way to guess.
        await throttle.record(session, user.email, ip, succeeded=False)
        await session.commit()
        return problem(
            401,
            "invalid_code",
            "That code didn't work",
            detail="Check the time on your device, then try the current code.",
        )
    await throttle.record(session, user.email, ip, succeeded=True)
    await record(
        session, entity_type="app_user", entity_id=user.id, action="totp_enrolled",
        actor_type=ActorType.HUMAN, actor_id=user.id,
    )
    body = await _session_body(session, user, device)
    body["recoveryCodes"] = codes  # shown once by the client, as Settings shows them once
    await session.commit()
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


async def _person(request: Request, session: AsyncSession) -> AppUser | None:
    """The person a deletion request speaks for: a native session or a D3 Auth token.

    Never an API token — a script is not anybody, and deleting an account is a person's decision
    — and never a browser session, whose token is a cookie the app does not hold.
    """
    token = _bearer(request)
    if not token or token.startswith(api_tokens.PREFIX):
        return None
    if d3auth_bearer.looks_like_provider_token(token):
        try:
            return await d3auth_bearer.user_for(session, token, resource=_base(request))
        except (d3auth_bearer.IdentityNotLinked, d3auth_bearer.NotD3AuthToken):
            return None
    try:
        claims = decode_access_claims(token)
    except TokenError:
        return None
    row = await session.get(RefreshToken, claims.session_id)
    if row is None or row.revoked_at is not None or row.device_name is None:
        return None
    user = await session.get(AppUser, claims.user_id)
    return user if user is not None and user.is_active else None


@router.post("/delete-account")
async def delete_account(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """Delete your own account from the app (BND-T-23.3, BND-ADR-015).

    The step-up is in the request: the host name typed out and a current code. Checked in the
    contract's order — who is asking, what they sent, the confirmation, the throttle, the code,
    and only then whether the account is the last owner of something, so a wrong code never
    learns anything about the instance. On success the account is disabled at once and removed
    after a grace period an administrator can use to restore it.
    """
    user = await _person(request, session)
    if user is None:
        return problem(401, "session_revoked", "Sign in again to delete this account")
    try:
        body = DeleteAccount.model_validate(await request.json())
    except (ValueError, ValidationError):
        return problem(400, None, "That request is not a deletion")

    host = urlsplit(_base(request)).hostname or ""
    if body.confirmation.strip().lower() != host.lower():
        return problem(
            422, None, "The confirmation doesn't match", detail=f"Type {host} exactly to confirm."
        )

    ip = client_ip(request)
    try:
        await throttle.check(session, user.email, ip)
    except throttle.Throttled as limited:
        await session.commit()
        return _throttled(limited)
    if not user.totp_enabled or not user.totp_secret:
        return problem(
            422,
            None,
            "This account has no authenticator",
            detail="Set one up in Bindery's Settings first: deleting an account needs a "
            "current code.",
        )
    # A code from the authenticator, never a recovery code: the step-up proves the device the
    # person holds. Burned like a sign-in code, so the same one cannot be replayed.
    step = totp_codes.verify(user.totp_secret, body.totp.strip(), after_step=user.totp_last_step)
    if step is None:
        await throttle.record(session, user.email, ip, succeeded=False)
        await session.commit()
        return problem(401, "invalid_code", "That code didn't work")
    user.totp_last_step = step
    await throttle.record(session, user.email, ip, succeeded=True)

    refused = await account_deletion.last_owner(session, user)
    if refused is not None:
        await session.commit()  # the code is spent either way
        return problem(
            409, "last_owner", "This account can't be deleted yet", detail=refused.detail
        )

    scheduled = await account_deletion.schedule(session, user)
    await record(
        session, entity_type="app_user", entity_id=user.id, action="deletion_requested",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        after={
            "via": "native",
            "delete_after": scheduled.grace_until.isoformat(),
            "sessions_ended": scheduled.sessions_ended,
            "api_tokens_revoked": scheduled.api_tokens_revoked,
            "push_forgotten": scheduled.push_forgotten,
        },
    )
    await events.publish(session, [events.Topic.SETTINGS])
    await session.commit()
    return JSONResponse(
        {"graceUntil": scheduled.grace_until.isoformat().replace("+00:00", "Z")},
        status_code=202,
    )
