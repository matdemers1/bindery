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

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api import accounts, oidc
from api.audit import record
from api.auth import service, throttle
from api.auth.client import client_ip
from api.auth.tokens import TokenError, access_ttl_minutes, decode_access_claims
from api.config import get_settings
from api.db.enums import ActorType
from api.db.models import AppUser
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


class Refresh(BaseModel):
    refreshToken: str


def _base(request: Request) -> str:
    """The server's own origin, as the client reached it (uvicorn trusts the proxy's
    X-Forwarded-Proto, so this is https behind the tunnel)."""
    return str(request.base_url).rstrip("/")


@well_known.get("/.well-known/d3-app.json", include_in_schema=False)
async def manifest(request: Request) -> JSONResponse:
    """The D3 App manifest (BND-T-22.1): what this server is, what it offers, where to sign in."""
    base = _base(request)
    build = build_of_this_process()
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
            "link": None,
            "inviteAccept": None,
            "deleteAccount": None,
            "relayRegister": None,
        },
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _challenge_for(user: AppUser, device: Device | None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "typ": CHALLENGE_TYPE,
        "sub": str(user.id),
        "dev": [device.name, device.platform] if device else None,
        "iat": int(now.timestamp()),
        "exp": int((now + CHALLENGE_TTL).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _read_challenge(token: str) -> tuple[uuid.UUID, tuple[str, str] | None]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("typ") != CHALLENGE_TYPE:
        raise TokenError("not a sign-in challenge")
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


async def _live_user(request: Request, session: AsyncSession) -> tuple[AppUser, uuid.UUID] | None:
    token = _bearer(request)
    if not token:
        return None
    try:
        claims = decode_access_claims(token)
    except TokenError:
        return None
    if not await service.session_is_live(session, claims.session_id):
        return None
    user = await session.get(AppUser, claims.user_id)
    return (user, claims.session_id) if user is not None and user.is_active else None


@router.post("/revoke", status_code=204)
async def revoke(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """Sign out: the session the Bearer token names ends here, now."""
    live = await _live_user(request, session)
    if live is None:
        return problem(401, "session_revoked", "This sign-in has ended")
    user, session_id = live
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
    live = await _live_user(request, session)
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
