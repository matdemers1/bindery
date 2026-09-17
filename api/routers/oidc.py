"""Sign in with D3 Auth: the routes (Phase 20, REQ-203 … REQ-209).

Four unauthenticated routes and two that need a session. The rules they apply live in
`api/oidc.py`; what is here is the HTTP shape, the cookies, and the order of the checks.

The client library is imported where it is used rather than at module import, so an archive that
has never configured SSO — which is every archive until an operator says otherwise — neither
needs the dependency present nor pays for it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api import oidc
from api.audit import record
from api.auth import service, throttle
from api.auth.client import client_ip
from api.auth.cookies import set_auth_cookies
from api.auth.dependencies import current_user
from api.auth.passwords import verify_password_async
from api.config import get_settings
from api.db.enums import ActorType
from api.db.models import AppUser, OidcLogoutEvent
from api.db.models.user import RefreshToken
from api.db.session import get_session
from api.schemas import OidcLinkOut, OidcStatusOut

log = logging.getLogger("bindery.oidc")

router = APIRouter(prefix="/auth/oidc", tags=["auth"])

#: Where the half-finished sign-in lives. Scoped to the callback, so it is not sent with any
#: other request, and short-lived, so one left on a shared machine is worthless.
TRANSACTION_COOKIE = "bindery_oidc_tx"
CALLBACK_PATH = "/api/auth/oidc/callback"

#: The throttle key for sign-ins that arrive this way. Shares the per-address budget with the
#: password form: a provider is not a way around the rate limit.
THROTTLE_KEY = "oidc:callback"


def _redirect_uri(request: Request) -> str:
    """Exactly what is registered at the provider — matched character for character there."""
    configured = (get_settings().oidc_redirect_uri or "").strip()
    return configured or str(request.url_for("oidc_callback"))


async def _client(session: AsyncSession) -> Any:
    """The SDK client for the configured provider, or a refusal saying which is missing."""
    configuration = await oidc.config(session)
    if not configuration.enabled:
        raise oidc.SsoDisabled()
    try:
        from d3auth_client import D3AuthClient
    except ModuleNotFoundError as missing:  # pragma: no cover - only in an image without it
        log.error("SSO is configured but d3auth-client is not installed")
        raise oidc.SsoUnavailable() from missing
    return D3AuthClient(
        issuer=configuration.issuer,
        client_id=configuration.client_id,
        client_secret=configuration.client_secret,
        sso_mode=configuration.mode,
    )


# ---------------------------------------------------------------------------
# What the sign-in screen asks before it draws a button
# ---------------------------------------------------------------------------


@router.get("/status", response_model=OidcStatusOut)
async def oidc_status(session: AsyncSession = Depends(get_session)) -> OidcStatusOut:
    """Whether to offer SSO, and whether it would work right now (REQ-209).

    Unauthenticated, because the sign-in screen is. It says the mode and whether the provider
    answers, and nothing else: no client id, no secret, and no issuer for an archive that has
    SSO off — a screen should not name a provider its operator has not configured.
    """
    configuration = await oidc.config(session)
    if not configuration.enabled:
        return OidcStatusOut(mode="off", ready=False, issuer=None)

    ready = False
    try:
        client = await _client(session)
        ready = bool(client.healthy())
    except (oidc.SsoDisabled, oidc.SsoUnavailable):
        ready = False
    return OidcStatusOut(mode=configuration.mode, ready=ready, issuer=configuration.issuer)


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


async def _begin(
    request: Request, session: AsyncSession, *, linking: AppUser | None
) -> RedirectResponse:
    """Begin a sign-in, and put everything the callback must remember in one cookie.

    The verifier, state and nonce go into a signed, short-lived cookie scoped to the callback —
    never a table keyed on `state`, which is readable by whoever supplies the state (finding
    F-12). `linking` records which account asked, so the callback attaches the identity to that
    account instead of signing somebody in.
    """
    try:
        client = await _client(session)
    except oidc.SsoDisabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found") from None
    except oidc.SsoUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "sign-in is unavailable") from None

    start = await client.start_sign_in(_redirect_uri(request))
    response = RedirectResponse(start.url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        TRANSACTION_COOKIE,
        oidc.seal_transaction(
            {
                "verifier": start.verifier,
                "state": start.state,
                "nonce": start.nonce,
                "link": str(linking.id) if linking else None,
            }
        ),
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        max_age=oidc.TRANSACTION_TTL_SECONDS,
        path=CALLBACK_PATH,
    )
    return response


@router.get("/start")
async def oidc_start(
    request: Request, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    """Begin a sign-in. Anonymous, because the sign-in screen is."""
    return await _begin(request, session, linking=None)


@router.get("/link/start")
async def oidc_link_start(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> RedirectResponse:
    """Begin a link for the account already signed in here (REQ-206).

    Separate from `/start` and behind a session on purpose: linking anonymously would be a way
    to attach an identity to an account that never asked for it.
    """
    return await _begin(request, session, linking=user)


@router.get("/callback", name="oidc_callback")
async def oidc_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Finish a sign-in: verify, find or provision the account, and mint a Bindery session."""
    ip = client_ip(request)
    try:
        await throttle.check(session, THROTTLE_KEY, ip)
    except throttle.Throttled as limited:
        await session.commit()
        return _back_to_sign_in("too-many-attempts", retry_after=limited.retry_after)

    try:
        client = await _client(session)
    except oidc.SsoDisabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found") from None
    except oidc.SsoUnavailable:
        return _back_to_sign_in("unavailable")

    try:
        transaction = oidc.open_transaction(request.cookies.get(TRANSACTION_COOKIE))
        from d3auth_client import SignInStart

        result = await client.finish_sign_in(
            str(request.url),
            start=SignInStart(
                url="",
                verifier=transaction["verifier"],
                state=transaction["state"],
                nonce=transaction["nonce"],
            ),
            redirect_uri=_redirect_uri(request),
        )
    except oidc.SignInRefused as refused:
        await throttle.record(session, THROTTLE_KEY, ip, succeeded=False)
        await session.commit()
        return _back_to_sign_in("expired", detail=refused.reason)
    except Exception as failure:  # the SDK raises ValueError for every refusal it makes
        await throttle.record(session, THROTTLE_KEY, ip, succeeded=False)
        await session.commit()
        log.warning("a D3 Auth callback was refused: %s", failure)
        return _back_to_sign_in("refused")

    claims = dict(result.identity.claims or {})
    try:
        user = await _account_for(
            session,
            transaction=transaction,
            issuer=result.identity.iss,
            subject=result.identity.sub,
            claims=claims,
            roles=result.identity.roles,
            refresh_token=result.refresh_token,
        )
    except oidc.SignInRefused as refused:
        await throttle.record(session, THROTTLE_KEY, ip, succeeded=False)
        await session.commit()
        return _back_to_sign_in("refused", detail=refused.reason)

    if transaction.get("link"):
        # A link, not a sign-in: the browser already has a session, and that session stays.
        await session.commit()
        return _to("/settings?linked=1", clear_transaction=True)

    await oidc.apply_roles(session, user=user, roles=result.identity.roles)
    await throttle.record(session, THROTTLE_KEY, ip, succeeded=True)

    access_token, refresh_secret = await service.issue_session(session, user)
    # The provider's session id rides on the Bindery session it produced, so a back-channel
    # logout naming that `sid` ends this one and not the person's other devices.
    await _remember_sid(session, user=user, sid=result.sid)
    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="login",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"via": "d3auth", "issuer": result.identity.iss, "roles": result.identity.roles},
    )
    await session.commit()

    response = _to("/", clear_transaction=True)
    set_auth_cookies(response, access_token, refresh_secret)
    return response


async def _account_for(
    session: AsyncSession,
    *,
    transaction: dict[str, Any],
    issuer: str,
    subject: str,
    claims: dict[str, Any],
    roles: list[str],
    refresh_token: str | None,
) -> AppUser:
    """The account this identity belongs to: linked, being linked, or newly provisioned."""
    identity = await oidc.identity_for(session, issuer=issuer, subject=subject)

    if linking_id := transaction.get("link"):
        user = await session.get(AppUser, uuid.UUID(linking_id))
        if user is None:
            raise oidc.SignInRefused("that account no longer exists")
        if identity is not None and identity.user_id != user.id:
            raise oidc.SignInRefused("that D3 Auth account is already connected to somebody here")
        if identity is None:
            await oidc.link(
                session,
                user=user,
                issuer=issuer,
                subject=subject,
                preferred_username=claims.get("preferred_username"),
                refresh_token=refresh_token,
                origin="link",
            )
        return user

    if identity is not None:
        user = await session.get(AppUser, identity.user_id)
        if user is None or not user.is_active or user.suspended_at is not None:
            raise oidc.SignInRefused("that account cannot sign in here")
        identity.last_seen_at = datetime.now(UTC)
        identity.refresh_token_enc = oidc.encrypt_token(refresh_token) or identity.refresh_token_enc
        return user

    # Nobody here yet. A role is an administrator's decision that already happened.
    return await oidc.provision(
        session,
        issuer=issuer,
        subject=subject,
        email=str(claims.get("email") or ""),
        display_name=str(claims.get("name") or "") or None,
        preferred_username=claims.get("preferred_username"),
        roles=roles,
        refresh_token=refresh_token,
    )


async def _remember_sid(session: AsyncSession, *, user: AppUser, sid: str | None) -> None:
    """Tag the refresh row just issued with the provider session it came from."""
    if not sid:
        return
    newest = (
        await session.execute(
            sa.select(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .order_by(RefreshToken.issued_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if newest is not None:
        newest.oidc_sid = sid


def _to(path: str, *, clear_transaction: bool = False) -> RedirectResponse:
    response = RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)
    if clear_transaction:
        response.delete_cookie(TRANSACTION_COOKIE, path=CALLBACK_PATH)
    return response


def _back_to_sign_in(
    reason: str, *, detail: str | None = None, retry_after: int | None = None
) -> RedirectResponse:
    """Back to the sign-in screen, which says what happened in its own words.

    The reason is a short code rather than a sentence: the screen owns the wording, and a
    message in a URL is a message an attacker can choose.
    """
    query = {"sso": reason}
    if retry_after:
        query["retry_after"] = str(retry_after)
    if detail:
        log.info("D3 Auth sign-in refused: %s", detail)
    return _to(f"/?{urlencode(query)}", clear_transaction=True)


# ---------------------------------------------------------------------------
# The Settings card
# ---------------------------------------------------------------------------


@router.get("/link", response_model=OidcLinkOut)
async def oidc_link_state(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> OidcLinkOut:
    """What the *Connect D3 Auth* card shows for the signed-in account."""
    configuration = await oidc.config(session)
    identity = await oidc.identity_of(session, user=user)
    return OidcLinkOut(
        mode=configuration.mode if configuration.enabled else "off",
        issuer=configuration.issuer if configuration.enabled else None,
        linked=identity is not None,
        preferred_username=identity.preferred_username if identity else None,
        linked_at=identity.linked_at if identity else None,
    )


@router.post("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def oidc_disconnect(
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> Response:
    """Disconnect the provider. The local password is the proof (REQ-206).

    Without it, a stolen session could quietly detach the account from the thing that revokes
    its access — or, on an account provisioned through SSO, lock it out of itself.
    """
    if not await verify_password_async(user.password_hash, password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that password is not right")
    if not await oidc.disconnect(session, user=user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this account is not connected")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Back-channel logout
# ---------------------------------------------------------------------------


@router.post("/backchannel-logout")
async def backchannel_logout(
    logout_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """End the sessions a logout token names (REQ-208).

    Unauthenticated by design: the token *is* the authentication. It is verified against the
    provider's keys, and applied at most once — `jti` is remembered in Postgres rather than in
    this process, because a retry that lands on a different process must not end a session the
    person has since started again.

    A repeat is a 200. Anything else makes the provider retry for nothing and then mark this app
    as slow to revoke, which is the state the endpoint exists to avoid.
    """
    configuration = await oidc.config(session)
    if not configuration.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    try:
        from d3auth_client import verify_logout_token
    except ModuleNotFoundError:  # pragma: no cover
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable") from None

    try:
        verified = verify_logout_token(
            logout_token, issuer=configuration.issuer, client_id=configuration.client_id
        )
    except Exception as refused:
        log.warning("a logout token was refused: %s", refused)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "that logout token was not accepted"
        ) from refused

    already = (
        await session.execute(sa.select(OidcLogoutEvent).where(OidcLogoutEvent.jti == verified.jti))
    ).scalar_one_or_none()
    if already is not None:
        return Response(status_code=status.HTTP_200_OK)

    ended = await oidc.end_sessions(
        session, issuer=configuration.issuer, subject=verified.sub, sid=verified.sid
    )
    session.add(
        OidcLogoutEvent(
            jti=verified.jti, subject=verified.sub, sid=verified.sid, ended_sessions=ended
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_200_OK)
