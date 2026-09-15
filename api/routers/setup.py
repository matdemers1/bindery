"""First-run setup: claim a fresh install in the browser (Phase 19, REQ-200/201).

Two of these three routes are unauthenticated and internet-facing — the tunnel
makes a fresh instance reachable before anyone has an account — so they reveal
exactly one fact, "is this archive claimed", and the claim is throttled by the
same limiter as the login form. The logic lives in `api/first_run.py`.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import accounts, first_run
from api.audit import record
from api.auth import service, throttle
from api.auth.client import client_ip
from api.auth.cookies import set_auth_cookies
from api.auth.dependencies import current_user
from api.auth.passwords import WeakPassword
from api.db.enums import ActorType
from api.db.models import AppUser
from api.db.session import get_session
from api.schemas import SetupClaimIn, SetupStateOut, UserOut

router = APIRouter(prefix="/setup", tags=["setup"])

# The throttle's account key for claims. No account has this address, so only
# the per-IP half of the limiter applies — which is the half that matters here,
# and it shares its count with the login form: a host guessing setup codes and
# passwords is one host guessing.
THROTTLE_KEY = "setup:claim"


@router.get("", response_model=SetupStateOut)
async def setup_state(session: AsyncSession = Depends(get_session)) -> SetupStateOut:
    return SetupStateOut(state=(await first_run.state(session)).value)


@router.post("/claim", response_model=UserOut)
async def claim(
    body: SetupClaimIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> AppUser:
    """Create the first account with the printed setup code, and sign it in."""
    ip = client_ip(request)
    try:
        await throttle.check(session, THROTTLE_KEY, ip)
    except throttle.Throttled as limited:
        await session.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again shortly.",
            headers={"Retry-After": str(limited.retry_after)},
        ) from limited

    try:
        claimed = await first_run.claim(
            session,
            code=body.code,
            email=body.email,
            password=body.password,
            display_name=body.display_name,
            library_name=body.library_name,
        )
    except first_run.AlreadyClaimed as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except WeakPassword as weak:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(weak)) from weak
    except accounts.AccountError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    except first_run.InvalidCode as error:
        # Committed: the failed attempt must survive, or the code is the
        # unthrottled half of setup. Nothing else is in this transaction —
        # `claim` writes nothing until the code has been verified — and not
        # rolling back keeps the throttle's per-address lock held until the
        # attempt row is committed, which is what stops a burst.
        await throttle.record(session, THROTTLE_KEY, ip, succeeded=False)
        await session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, first_run.INVALID_CODE) from error

    user = claimed.user
    await throttle.record(session, THROTTLE_KEY, ip, succeeded=True)
    access_token, refresh_secret = await service.issue_session(session, user)
    await record(
        session, entity_type="app_user", entity_id=user.id, action="account_created",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        after={
            "via": "setup",
            "setup_owner": True,
            "email": user.email,
            "library": claimed.library.name,
            "library_id": str(claimed.library.id),
        },
    )
    await session.commit()
    set_auth_cookies(response, access_token, refresh_secret)
    return user


@router.post("/complete", response_model=UserOut)
async def complete(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> AppUser:
    """Finish setup: the claiming account, with TOTP enrolled, becomes administrator."""
    try:
        await first_run.complete(session, user=user)
    except first_run.SetupError as error:
        code = (
            status.HTTP_403_FORBIDDEN if error.kind == "not_owner" else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, str(error)) from error
    except accounts.AccountError as error:
        # grant_admin's refusal without two-factor (REQ-156), verbatim.
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    await record(
        session, entity_type="app_user", entity_id=user.id, action="admin_granted",
        actor_type=ActorType.HUMAN, actor_id=user.id, after={"via": "setup"},
    )
    await session.commit()
    return user
