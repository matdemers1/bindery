"""Push registration for D3 Constellation (BND-T-23.4, the D3 App contract's push).

The app registers this connection at the relay, then tells Bindery where to send and the key to seal
to. Answered 204, then one bindery.registered notification — "Notifications are on" — so the person
knows push works. Only a native session or a D3 Auth token registers: never the browser's cookie,
never an API token.
"""

import base64
import binascii
from datetime import UTC, datetime
from urllib.parse import urlsplit

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api import push
from api.audit import record
from api.config import get_settings
from api.db.enums import ActorType
from api.db.models import OidcIdentity, RelayRegistration
from api.db.session import SessionFactory, get_session
from api.problems import problem
from api.routers.native import _live_user, _NotLinked

router = APIRouter(prefix="/push", tags=["push"])

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class Relay(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    registration: str = Field(min_length=1, max_length=200)
    sendKey: str = Field(min_length=16, max_length=200)


class Register(BaseModel):
    devicePublicKey: str = Field(min_length=1, max_length=200)
    relay: Relay
    categories: list[str] = Field(default_factory=list, max_length=20)


def _relay_url_ok(raw: str) -> bool:
    """https, or a loopback http relay where the test configuration allows one — never in prod."""
    try:
        url = urlsplit(raw)
    except ValueError:
        return False
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        return False
    if url.scheme == "https":
        return True
    return (
        url.scheme == "http"
        and get_settings().relay_allow_loopback_http
        and url.hostname in LOOPBACK
    )


async def _welcome(row_id) -> None:
    """The first notification, after the answer: a relay that is down must not fail the request."""
    async with SessionFactory() as session:
        row = await session.get(RelayRegistration, row_id)
        if row is not None:
            await push.push(
                session,
                row,
                {
                    "v": 1,
                    "category": "bindery.registered",
                    "title": "Notifications are on",
                    "body": "Bindery will tell this device what needs it.",
                    "sentAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
            )


@router.post("/native/register", status_code=204)
async def register(
    payload: dict,
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        live = await _live_user(request, session)
    except _NotLinked:
        return problem(401, "identity_not_linked", "Link this D3 Auth account first")
    if live is None:
        return problem(401, "session_revoked", "Sign in again")
    user, session_id = live
    try:
        body = Register.model_validate(payload)
        raw_key = base64.b64decode(body.devicePublicKey, validate=True)
    except (ValueError, binascii.Error):
        return problem(400, None, "That is not a relay registration")
    if push.device_key(raw_key) is None or not _relay_url_ok(body.relay.url):
        return problem(400, None, "That is not a relay registration")
    if any(
        not category.replace(".", "").replace("_", "").isalnum() for category in body.categories
    ):
        return problem(400, None, "That is not a relay registration")

    identity_id = None
    if session_id is None:
        # A D3 Auth token: no Bindery session behind it, so the registration belongs to the link.
        identity = (
            await session.execute(
                sa.select(OidcIdentity).where(
                    OidcIdentity.user_id == user.id, OidcIdentity.unlinked_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if identity is None:
            return problem(401, "identity_not_linked", "Link this D3 Auth account first")
        identity_id = identity.id

    relay_url = body.relay.url.rstrip("/")
    row = await push.register(
        session,
        user_id=user.id,
        session_id=session_id,
        identity_id=identity_id,
        device_public_key=raw_key,
        relay_url=relay_url,
        registration=body.relay.registration,
        send_key=body.relay.sendKey,
        categories=body.categories,
    )
    await record(
        session,
        entity_type="relay_registration",
        entity_id=row.id,
        action="push_registered",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={
            "relay": relay_url,
            "categories": body.categories,
            "owner": "session" if session_id else "identity",
        },
    )
    await session.commit()
    background.add_task(_welcome, row.id)
    return Response(status_code=204)
