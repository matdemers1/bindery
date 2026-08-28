from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.auth import service
from api.auth.cookies import REFRESH_COOKIE, clear_auth_cookies, set_auth_cookies
from api.auth.dependencies import current_user
from api.db.enums import ActorType
from api.db.models import AppUser
from api.db.session import get_session
from api.schemas import LoginRequest, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=UserOut)
async def login(
    payload: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> AppUser:
    try:
        user = await service.authenticate(session, payload.email, payload.password)
    except service.AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

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
