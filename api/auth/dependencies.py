"""FastAPI dependencies for the authenticated caller."""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import tokens as api_tokens
from api.auth.cookies import ACCESS_COOKIE
from api.auth.tokens import TokenError, decode_access_token
from api.db.models import AppUser
from api.db.scope import Scope, resolve
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

    if token.startswith(api_tokens.PREFIX):
        # A scoped API token (REQ-107). Stashed on the request so `current_scope`
        # can narrow the boundary to what this particular token may reach —
        # a token must never be able to do more than a browser session, and
        # usually does less.
        identity = await api_tokens.verify(session, token)
        if identity is None:
            raise _UNAUTHENTICATED
        request.state.api_token = identity
        user_id = identity.user_id
    else:
        try:
            user_id = decode_access_token(token)
        except TokenError as exc:
            raise _UNAUTHENTICATED from exc

    user = await session.get(AppUser, user_id)
    if user is None or not user.is_active:
        raise _UNAUTHENTICATED
    return user


async def current_scope(
    request: Request,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Scope:
    """The caller's permission boundary (REQ-101).

    Injected instead of hand-rolling `visible_library_ids` in each route, so a
    route cannot forget the filter: the only queries a `Scope` will build are
    already constrained.

    When the caller is an API token, the boundary is narrowed to the token's
    libraries — never widened. A token is always a subset of its owner.
    """
    scope = await resolve(session, user.id)

    identity = getattr(request.state, "api_token", None)
    if identity is not None:
        permitted = set(identity.library_ids)
        scope = Scope(
            user_id=scope.user_id,
            visible=tuple(lid for lid in scope.visible if lid in permitted),
            writable=(
                tuple(lid for lid in scope.writable if lid in permitted)
                if identity.allows("upload") or identity.allows("admin")
                else ()
            ),
            roles={lid: role for lid, role in scope.roles.items() if lid in permitted},
        )

    scope.require_any()
    return scope


def require_scope(scope_name: str):
    """Refuse a token that was not granted this capability.

    A no-op for cookie sessions: a person signed in through the browser is
    already bounded by their memberships, and scopes exist to make a *token*
    narrower than the person who issued it.
    """

    async def dependency(request: Request, user: AppUser = Depends(current_user)) -> None:
        identity = getattr(request.state, "api_token", None)
        if identity is None:
            return
        if not identity.allows(scope_name):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"this token does not have the {scope_name!r} scope",
            )

    return dependency
