"""FastAPI dependencies for the authenticated caller."""

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import eventlog
from api import tokens as api_tokens
from api.auth import service as auth_service
from api.auth.cookies import ACCESS_COOKIE
from api.auth.tokens import TokenError, decode_access_claims
from api.db import repository
from api.db.models import AppUser
from api.db.scope import Scope, resolve
from api.db.session import get_session

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated"
)

# The vault surface, which no token may reach at all (ADR-012). Unlocking is
# something a person did at a keyboard with a PIN, and a long-lived bearer
# token is the opposite of that — so the refusal is here, in front of every
# vault route, rather than in a router that has to remember to ask.
# `tests/test_api_tokens.py` asserts the prefix still names those routes.
VAULT_PATH = "/api/vault"

# Account administration, which needs the scope that means "everything the
# creating user can do" rather than merely `read` — inviting, suspending and
# reading the account list are not what a scanner or an export script is for.
# The same test asserts every `require_admin` route is under this prefix.
ADMIN_PATH = "/api/admin"

# A GET is a read and anything else changes something. That is the only signal
# the auth layer has that does not depend on twenty routers each declaring a
# capability, and the last thing that depended on them declaring it — a
# `require_scope` dependency — was wired to no route at all.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# `read` is deliberately not among them: a read-only token that can upload,
# reclassify or mint another token is the whole defect.
_WRITING_SCOPES = ("upload", "export", "admin")


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value or None if scheme.lower() == "bearer" else None


def _refuse_beyond_the_token(request: Request, identity: api_tokens.TokenIdentity) -> None:
    """Refuse a request the token's capabilities do not cover."""
    if request.url.path.startswith(VAULT_PATH):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "the vault is not reachable with an API token"
        )

    if request.url.path.startswith(ADMIN_PATH) and not identity.allows("admin"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "this token does not have the 'admin' scope"
        )

    if request.method in _READ_METHODS:
        if not identity.allows("read"):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "this token does not have the 'read' scope"
            )
    elif not any(identity.allows(scope) for scope in _WRITING_SCOPES):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this token may only read")


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> AsyncIterator[AppUser]:
    """The signed-in user, from the HTTP-only cookie or a bearer token.

    The bearer path is what lets a scoped API token work without a browser flow
    (REQ-105); Cloudflare Access sits in front of it as defence in depth, never
    as the perimeter.
    """
    token = request.cookies.get(ACCESS_COOKIE) or _bearer_token(request)
    if not token:
        raise _UNAUTHENTICATED

    identity: api_tokens.TokenIdentity | None = None
    if token.startswith(api_tokens.PREFIX):
        # A scoped API token (REQ-107). Stashed on the request so `current_scope`
        # can narrow the boundary to what this particular token may reach —
        # a token must never be able to do more than a browser session, and
        # usually does less.
        identity = await api_tokens.verify(session, token)
        if identity is None:
            raise _UNAUTHENTICATED
        request.state.api_token = identity
        _refuse_beyond_the_token(request, identity)
        user_id = identity.user_id
    else:
        try:
            claims = decode_access_claims(token)
        except TokenError as exc:
            raise _UNAUTHENTICATED from exc
        # The session behind the token, checked rather than assumed. A signed
        # JWT survives logout, a password reset, a redeemed reset code and a
        # suspension for its full lifetime unless something looks — and those
        # are precisely the moments somebody is trying to end a session they
        # believe is not theirs.
        if not await auth_service.session_is_live(session, claims.session_id):
            raise _UNAUTHENTICATED
        user_id = claims.user_id

    user = await session.get(AppUser, user_id)
    if user is None or not user.is_active:
        raise _UNAUTHENTICATED

    # Everything this request logs is tagged with who caused it. Some log lines
    # legitimately have no library — an import scan naming a folder, a failed
    # login — and those used to be visible to every signed-in user (REQ-144).
    eventlog.bind_for_request(user_id=user.id)

    # Set on every authenticated request, including to `None` for a browser
    # session: a boundary that is only ever narrowed when someone remembers to
    # narrow it is the defect this closes.
    repository.bind_token_boundary(
        repository.TokenBoundary(
            user_id=user.id,
            library_ids=frozenset(identity.library_ids),
            may_write=identity.allows("upload") or identity.allows("admin"),
        )
        if identity is not None
        else None
    )
    try:
        yield user
    finally:
        # A finished request leaves nothing behind for whatever runs next in
        # this context.
        repository.bind_token_boundary(None)


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
