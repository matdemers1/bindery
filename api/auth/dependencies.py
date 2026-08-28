"""FastAPI dependencies for the authenticated caller."""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.cookies import ACCESS_COOKIE
from api.auth.tokens import TokenError, decode_access_token
from api.db.models import AppUser
from api.db.session import get_session

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated"
)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value or None if scheme.lower() == "bearer" else None


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> AppUser:
    """The signed-in user, from the HTTP-only cookie or a bearer token.

    The bearer path is what lets a scoped API token work without a browser flow
    (REQ-105); Cloudflare Access sits in front of it as defence in depth, never
    as the perimeter.
    """
    token = request.cookies.get(ACCESS_COOKIE) or _bearer_token(request)
    if not token:
        raise _UNAUTHENTICATED
    try:
        user_id = decode_access_token(token)
    except TokenError as exc:
        raise _UNAUTHENTICATED from exc

    user = await session.get(AppUser, user_id)
    if user is None or not user.is_active:
        raise _UNAUTHENTICATED
    return user
