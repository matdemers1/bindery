import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import accounts, first_run, oidc
from api.audit import record
from api.auth import service, throttle
from api.auth.client import client_ip
from api.auth.cookies import ACCESS_COOKIE, REFRESH_COOKIE, clear_auth_cookies, set_auth_cookies
from api.auth.dependencies import current_user
from api.auth.tokens import TokenError, decode_access_claims
from api.db.enums import ActorType
from api.db.models import AppUser, RefreshToken
from api.db.session import get_session
from api.schemas import LoginRequest, SignedInSessionOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=UserOut)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> AppUser:
    ip = client_ip(request)
    try:
        await throttle.check(session, payload.email, ip)
    except throttle.Throttled as limited:
        # Committed so the attempt that triggered this is not lost when the
        # request unwinds.
        await session.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Try again shortly.",
            headers={"Retry-After": str(limited.retry_after)},
        ) from limited

    try:
        user = await service.authenticate(session, payload.email, payload.password)
    except service.AuthError as exc:
        await throttle.record(session, payload.email, ip, succeeded=False)
        await session.commit()
        # One message for every failure — no account, wrong password, disabled.
        # A response that distinguishes them turns the login form into a
        # membership oracle, and the membership here is a list of the owner's
        # family (REQ-135).
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from exc

    # The password was right. A missing or wrong second factor is still a
    # failed attempt as far as the throttle is concerned, or the code becomes
    # the unthrottled half of the login.
    factor = accounts.SecondFactor.NONE
    if user.totp_enabled:
        try:
            factor = await accounts.check_second_factor(session, user=user, code=payload.code or "")
        except accounts.AccountError as exc:
            await throttle.record(session, payload.email, ip, succeeded=False)
            await session.commit()
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "second factor required",
                # The only thing this reveals is revealed *after* the password
                # was correct, to someone who therefore already has the account.
                headers={"WWW-Authenticate": 'Bindery realm="totp"'},
            ) from exc

    await throttle.record(session, payload.email, ip, succeeded=True)

    if factor is accounts.SecondFactor.RECOVERY:
        await retire_after_recovery(session, user)

    access_token, refresh_secret = await service.issue_session(session, user)
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="login",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
    )
    await session.commit()

    set_auth_cookies(response, access_token, refresh_secret)
    return user


async def retire_after_recovery(session: AsyncSession, user: AppUser) -> None:
    """A recovery code got them in, so the authenticator is gone.

    Retire it here rather than ask for it again at the next sign-in, which is how somebody spends
    ten recovery codes and then needs a shell on the host. Shared by the browser sign-in and the
    native one (BND-T-22.2), so both leave the account in the same state.
    """
    retired = await accounts.retire_second_factor(session, user=user)
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="totp_disabled",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={
            "via": "recovery_code",
            "recovery_codes_superseded": retired.codes_superseded,
        },
    )
    if retired.admin_revoked:
        await record(
            session,
            entity_type="app_user",
            entity_id=user.id,
            action="admin_revoked",
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            before={"is_admin": True},
            after={"reason": "REQ-156: no second factor until it is enrolled again"},
        )
        await first_run.resume_at_second_factor(session, user=user)


@router.post("/refresh", response_model=UserOut)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> AppUser:
    """Rotate the refresh token. Presenting an already-used one ends every
    session the user holds — see service.rotate_session."""
    secret = request.cookies.get(REFRESH_COOKIE)
    if not secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no refresh token")

    try:
        access_token, refresh_secret, user = await service.rotate_session(session, secret)
    except service.AuthError as exc:
        # Commit first: a reuse attempt revoked the user's tokens, and that
        # revocation must survive the 401.
        await session.commit()
        clear_auth_cookies(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    # A renewal is also when a role granted elsewhere is re-read (Phase 20, REQ-207). Only for
    # an account linked to a provider, and never fatal: a provider that cannot be reached leaves
    # the roles as they were rather than signing anybody out.
    await oidc.refresh_roles(session, user=user)

    await session.commit()
    set_auth_cookies(response, access_token, refresh_secret)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> None:
    """End the session server-side, not merely on this browser.

    Deleting the cookies used to be all this did when the refresh cookie was
    absent — a same-site navigation, a client that dropped it, a curl session.
    "Sign out" then meant "forget my copy", and anyone who already had the
    access token carried on for the rest of its lifetime. The access token names
    its session (`sid`), so it can be revoked on its own.
    """
    secret = request.cookies.get(REFRESH_COOKIE)
    revoked = False
    if secret:
        await service.revoke_session(session, secret)
        revoked = True

    if not revoked:
        access = request.cookies.get(ACCESS_COOKIE)
        if access:
            try:
                claims = decode_access_claims(access)
            except TokenError:
                # Nothing to revoke, and nothing to say about it: a sign-out
                # never reports on the token it was handed.
                pass
            else:
                await service.revoke_session_by_id(session, claims.session_id)
                revoked = True

    if revoked:
        await session.commit()
    clear_auth_cookies(response)


@router.get("/me", response_model=UserOut)
async def me(user: AppUser = Depends(current_user)) -> AppUser:
    return user


def _current_session_id(request: Request) -> uuid.UUID | None:
    """The session the request itself rides on, so the list can say "this device"."""
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        token = value if scheme.lower() == "bearer" else None
    if not token:
        return None
    try:
        return decode_access_claims(token).session_id
    except TokenError:
        return None


def _refuse_api_tokens(request: Request) -> None:
    # Which devices are signed in, and ending them, is a person's business at a screen. A scanner's
    # token has no reason to read it and none to end somebody's phone session.
    if getattr(request.state, "api_token", None) is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "sessions are not reachable with an API token"
        )


@router.get("/sessions", response_model=list[SignedInSessionOut])
async def sessions(
    request: Request,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[SignedInSessionOut]:
    """Every session this person holds, newest activity first (BND-T-22.4) — browsers and the
    devices D3 Constellation signed in, which name themselves."""
    _refuse_api_tokens(request)
    current = _current_session_id(request)
    rows = (
        await session.execute(
            sa.select(RefreshToken)
            .where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > datetime.now(UTC),
            )
            .order_by(RefreshToken.issued_at.desc())
        )
    ).scalars()
    return [
        SignedInSessionOut(
            id=row.id,
            device_name=row.device_name,
            device_platform=row.device_platform,
            last_active=row.issued_at,
            current=row.id == current,
        )
        for row in rows
    ]


@router.post("/sessions/{session_id}/end", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(
    session_id: uuid.UUID,
    request: Request,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Sign one of your own sessions out — the phone you left on a train. Its next request, or
    its next refresh, finds it ended (`session_revoked` to a native client)."""
    _refuse_api_tokens(request)
    row = await session.get(RefreshToken, session_id)
    # Somebody else's session reads exactly like no session: the id is not a thing to probe.
    if row is None or row.user_id != user.id or row.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such session")
    await service.revoke_session_by_id(session, session_id)
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="session_ended",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"session": str(session_id), "device": row.device_name},
    )
    await session.commit()
