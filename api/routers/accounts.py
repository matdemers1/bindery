"""Account routes: invitations, enrolment, the admin panel (Phase 10b/10c).

Two boundaries are enforced here rather than described.

`require_admin` gates administration of *accounts*. It does not — and must never
— widen what its holder can read: there is no admin branch in
`visible_library_ids`, and every route in this file that returns anything about
another account returns counts, states and timestamps. Never a title, never a
filename, never a page (ADR-009, REQ-143).

The unauthenticated routes — accepting an invitation, redeeming a reset code —
are reachable by anyone on the internet since Cloudflare Access came off, and
are throttled by the same limiter as the login form for the same reason.
"""

import logging
import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import accounts, events, quota
from api.audit import record
from api.auth import service, throttle, totp
from api.auth.cookies import set_auth_cookies
from api.auth.dependencies import current_user
from api.auth.passwords import WeakPassword, verify_password
from api.db.enums import ActorType
from api.db.models import AppUser, Invitation
from api.db.session import get_session
from api.schemas import (
    AcceptInviteIn,
    AccountOut,
    AdminAccountOut,
    ChangePasswordIn,
    InviteIn,
    InviteOut,
    QuotaOut,
    RedeemResetIn,
    ResetCodeOut,
    TotpConfirmIn,
    TotpEnrolOut,
    TotpStatusOut,
    UserOut,
)

log = logging.getLogger("bindery.accounts")

router = APIRouter(tags=["accounts"])


async def require_admin(user: AppUser = Depends(current_user)) -> AppUser:
    """Administers accounts. Reads no documents — see ADR-009."""
    if not user.is_admin:
        # 404 rather than 403 for the same reason libraries do it: a 403
        # confirms the route exists and that someone somewhere is an admin.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return user


def _client_ip(request: Request) -> str | None:
    header = request.headers.get("cf-connecting-ip") or request.headers.get(
        "x-forwarded-for", ""
    ).split(",")[0].strip()
    return header or (request.client.host if request.client else None)


# --------------------------------------------------------------------------
# Me
# --------------------------------------------------------------------------


@router.get("/account", response_model=AccountOut)
async def my_account(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> AccountOut:
    usage = await quota.usage_for(session, user)
    return AccountOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        totp_enabled=user.totp_enabled,
        storage=QuotaOut(
            used_bytes=usage.used_bytes,
            quota_bytes=usage.quota_bytes,
            files=usage.files,
        ),
    )


@router.post("/account/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    body: ChangePasswordIn,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> Response:
    """Change your own password, ending every other session (REQ-137)."""
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is wrong")
    try:
        await accounts.change_password(session, user=user, new_password=body.new_password)
    except WeakPassword as weak:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(weak)) from weak

    await record(
        session, entity_type="app_user", entity_id=user.id, action="change_password",
        actor_type=ActorType.HUMAN, actor_id=user.id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Two-factor
# --------------------------------------------------------------------------


@router.get("/account/totp", response_model=TotpStatusOut)
async def totp_status(user: AppUser = Depends(current_user)) -> TotpStatusOut:
    return TotpStatusOut(enabled=user.totp_enabled, required=user.is_admin)


@router.post("/account/totp/start", response_model=TotpEnrolOut)
async def totp_start(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> TotpEnrolOut:
    """Generate a secret. It is not active until a code from it is verified."""
    if user.totp_enabled:
        # Starting an enrolment clears `totp_confirmed_at`, which is right while
        # there is nothing to lose and wrong the moment there is: it would turn
        # the live second factor off before anything had been proved, with no
        # password asked for, so a stolen session alone would strip it — and it
        # would walk straight past the refusal in `totp_disable` that exists so
        # an administrator is never left with a password alone (REQ-156).
        #
        # There is nowhere to hold a second, pending secret, so the only honest
        # answer while one is active is no. Replacing an authenticator means
        # turning the factor off first, which is what the screen offers and what
        # an administrator is deliberately not allowed to do.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "two-factor is already on for this account. Turn it off first, then "
            "set it up again with the new authenticator.",
        )
    secret = totp.new_secret()
    user.totp_secret = secret
    user.totp_confirmed_at = None
    user.totp_last_step = None
    await session.commit()
    return TotpEnrolOut(
        secret=secret, uri=totp.provisioning_uri(secret, email=user.email)
    )


@router.post("/account/totp/confirm", response_model=list[str])
async def totp_confirm(
    body: TotpConfirmIn,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> list[str]:
    """Prove a code and activate. Returns recovery codes, shown once."""
    try:
        codes = await accounts.confirm_totp(session, user=user, code=body.code)
    except accounts.AccountError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    await record(
        session, entity_type="app_user", entity_id=user.id, action="totp_enrolled",
        actor_type=ActorType.HUMAN, actor_id=user.id,
    )
    await session.commit()
    return codes


@router.delete("/account/totp", status_code=status.HTTP_204_NO_CONTENT)
async def totp_disable(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> Response:
    """Turn two-factor off. Refused for an administrator (REQ-156)."""
    if user.is_admin:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "an administrator cannot turn off two-factor: this account can reset "
            "every other password. Hand over administrator rights first.",
        )
    user.totp_secret = None
    user.totp_confirmed_at = None
    user.totp_last_step = None
    await record(
        session, entity_type="app_user", entity_id=user.id, action="totp_disabled",
        actor_type=ActorType.HUMAN, actor_id=user.id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Joining — unauthenticated, and internet-facing since ADR-008
# --------------------------------------------------------------------------


@router.get("/invitations/{token}", response_model=InviteOut)
async def preview_invitation(
    token: str, session: AsyncSession = Depends(get_session)
) -> InviteOut:
    """What this link is for, so the join page can say who it is addressed to."""
    try:
        invitation = await accounts.find_invitation(session, token)
    except accounts.AccountError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    return InviteOut(
        email=invitation.email,
        library_name=invitation.library_name,
        expires_at=invitation.expires_at,
        note=invitation.note,
        storage_quota_bytes=invitation.storage_quota_bytes,
    )


@router.post("/invitations/{token}/accept", response_model=UserOut)
async def accept_invitation(
    token: str,
    body: AcceptInviteIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> AppUser:
    """Create the account and sign it in."""
    ip = _client_ip(request)
    try:
        await throttle.check(session, f"invite:{token[:12]}", ip)
    except throttle.Throttled as limited:
        await session.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again shortly.",
            headers={"Retry-After": str(limited.retry_after)},
        ) from limited

    try:
        user = await accounts.accept(
            session, token=token, password=body.password, display_name=body.display_name
        )
    except WeakPassword as weak:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(weak)) from weak
    except accounts.AccountError as error:
        await throttle.record(session, f"invite:{token[:12]}", ip, succeeded=False)
        await session.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error

    access_token, refresh_secret = await service.issue_session(session, user)
    await record(
        session, entity_type="app_user", entity_id=user.id, action="account_created",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        after={"via": "invitation"},
    )
    await session.commit()
    set_auth_cookies(response, access_token, refresh_secret)
    return user


@router.post("/account/reset", status_code=status.HTTP_204_NO_CONTENT)
async def redeem_reset(
    body: RedeemResetIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Spend an administrator-issued code and set a new password (REQ-136)."""
    ip = _client_ip(request)
    try:
        await throttle.check(session, body.email, ip)
    except throttle.Throttled as limited:
        await session.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again shortly.",
            headers={"Retry-After": str(limited.retry_after)},
        ) from limited

    try:
        user = await accounts.redeem_reset_code(
            session, email=body.email, code=body.code, new_password=body.new_password
        )
    except WeakPassword as weak:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(weak)) from weak
    except accounts.AccountError as error:
        await throttle.record(session, body.email, ip, succeeded=False)
        await session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error

    await record(
        session, entity_type="app_user", entity_id=user.id, action="password_reset",
        actor_type=ActorType.HUMAN, actor_id=user.id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Administration — accounts only, never documents (ADR-009)
# --------------------------------------------------------------------------


@router.get("/admin/accounts", response_model=list[AdminAccountOut])
async def list_accounts(
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> list[AdminAccountOut]:
    """Every account, with what it holds — counts and bytes, never contents."""
    usage = {row["user_id"]: row for row in await quota.usage_by_account(session)}
    users = (
        await session.execute(sa.select(AppUser).order_by(AppUser.created_at))
    ).scalars().all()

    out = []
    for user in users:
        held = usage.get(user.id, {})
        out.append(
            AdminAccountOut(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                is_admin=user.is_admin,
                is_active=user.is_active,
                suspended_at=user.suspended_at,
                locked_until=user.locked_until,
                totp_enabled=user.totp_enabled,
                storage_quota_bytes=user.storage_quota_bytes,
                used_bytes=int(held.get("used_bytes", 0)),
                created_at=user.created_at,
            )
        )
    return out


@router.post("/admin/invitations", response_model=dict)
async def create_invitation(
    body: InviteIn,
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> dict:
    """Create an invitation. The link is returned exactly once."""
    try:
        issued = await accounts.invite(
            session,
            email=body.email,
            library_name=body.library_name,
            created_by=admin,
            storage_quota_bytes=body.storage_quota_bytes,
            note=body.note,
        )
    except accounts.AccountError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error

    await record(
        session, entity_type="invitation", entity_id=issued.invitation.id,
        action="invite_created", actor_type=ActorType.HUMAN, actor_id=admin.id,
        after={"email": body.email, "library": body.library_name},
    )
    await session.commit()
    return {
        "token": issued.token,
        "email": issued.invitation.email,
        "expires_at": issued.invitation.expires_at.isoformat(),
        # The caller builds the link; the API does not know the public hostname
        # and guessing it would produce a link that silently goes nowhere.
        "path": f"/join/{issued.token}",
    }


@router.get("/admin/invitations", response_model=list[dict])
async def list_invitations(
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> list[dict]:
    rows = (
        await session.execute(sa.select(Invitation).order_by(Invitation.created_at.desc()))
    ).scalars().all()
    return [
        {
            "id": str(row.id),
            "email": row.email,
            "library_name": row.library_name,
            "expires_at": row.expires_at.isoformat(),
            "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "note": row.note,
        }
        for row in rows
    ]


@router.delete("/admin/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> Response:
    invitation = await session.get(Invitation, invitation_id)
    if invitation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    from datetime import UTC, datetime

    invitation.revoked_at = datetime.now(UTC)
    await record(
        session, entity_type="invitation", entity_id=invitation.id,
        action="invite_revoked", actor_type=ActorType.HUMAN, actor_id=admin.id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/admin/accounts/{user_id}/reset-code", response_model=ResetCodeOut)
async def issue_reset_code(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> ResetCodeOut:
    """Issue a one-time code to hand over. Never sets a password (ADR-009)."""
    user = await session.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    code = await accounts.issue_reset_code(session, user=user, issued_by=admin)
    await record(
        session, entity_type="app_user", entity_id=user.id, action="reset_code_issued",
        actor_type=ActorType.HUMAN, actor_id=admin.id,
    )
    await session.commit()
    return ResetCodeOut(code=code, email=user.email, expires_in_hours=24)


@router.post("/admin/accounts/{user_id}/suspend", response_model=dict)
async def suspend_account(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> dict:
    user = await session.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    try:
        ended = await accounts.suspend(session, user=user, by=admin)
    except accounts.AccountError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    await record(
        session, entity_type="app_user", entity_id=user.id, action="account_suspended",
        actor_type=ActorType.HUMAN, actor_id=admin.id, after={"sessions_ended": ended},
    )
    await events.publish(session, [events.Topic.SETTINGS])
    await session.commit()
    return {"sessions_ended": ended}


@router.post("/admin/accounts/{user_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore_account(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> Response:
    user = await session.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    await accounts.restore(session, user=user)
    await record(
        session, entity_type="app_user", entity_id=user.id, action="account_restored",
        actor_type=ActorType.HUMAN, actor_id=admin.id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/admin/accounts/{user_id}/unlock", status_code=status.HTTP_204_NO_CONTENT)
async def unlock_account(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> Response:
    """Clear a lockout early (REQ-134). The lockout expires on its own anyway."""
    user = await session.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    user.locked_until = None
    await record(
        session, entity_type="app_user", entity_id=user.id, action="account_unlocked",
        actor_type=ActorType.HUMAN, actor_id=admin.id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/admin/accounts/{user_id}/quota", status_code=status.HTTP_204_NO_CONTENT)
async def set_quota(
    user_id: uuid.UUID,
    body: dict,
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> Response:
    user = await session.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    before = user.storage_quota_bytes
    user.storage_quota_bytes = body.get("storage_quota_bytes")
    await record(
        session, entity_type="app_user", entity_id=user.id, action="quota_changed",
        actor_type=ActorType.HUMAN, actor_id=admin.id,
        before={"storage_quota_bytes": before},
        after={"storage_quota_bytes": user.storage_quota_bytes},
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/admin/accounts/{user_id}/admin", status_code=status.HTTP_204_NO_CONTENT)
async def grant_admin(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: AppUser = Depends(require_admin),
) -> Response:
    """Grant administrator rights. Refused without two-factor (REQ-156)."""
    user = await session.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    try:
        await accounts.grant_admin(session, user=user)
    except accounts.AccountError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    await record(
        session, entity_type="app_user", entity_id=user.id, action="admin_granted",
        actor_type=ActorType.HUMAN, actor_id=admin.id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
