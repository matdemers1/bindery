from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.auth import service, throttle
from api.auth.cookies import REFRESH_COOKIE, clear_auth_cookies, set_auth_cookies
from api.auth.dependencies import current_user
from api.db.enums import ActorType
from api.db.models import AppUser
from api.db.session import get_session
from api.schemas import LoginRequest, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    """The address the request came from, as far as we can honestly tell.

    Behind the Cloudflare tunnel every request arrives from the tunnel
    container, so `request.client` is useless. `CF-Connecting-IP` is set by
    Cloudflare and cannot be spoofed by the client *because* the tunnel is the
    only route in — there is no published port to reach the origin directly
    (REQ-104). If that ever stops being true, this header stops being trustworthy
    and the throttle stops being per-attacker.
    """
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get(
        "x-forwarded-for", ""
    ).split(",")[0].strip()
    return forwarded or (request.client.host if request.client else None)


@router.post("/login", response_model=UserOut)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> AppUser:
    ip = _client_ip(request)
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
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid credentials"
        ) from exc

    await throttle.record(session, payload.email, ip, succeeded=True)

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

    await session.commit()
    set_auth_cookies(response, access_token, refresh_secret)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> None:
    secret = request.cookies.get(REFRESH_COOKIE)
    if secret:
        await service.revoke_session(session, secret)
        await session.commit()
    clear_auth_cookies(response)


@router.get("/me", response_model=UserOut)
async def me(user: AppUser = Depends(current_user)) -> AppUser:
    return user
