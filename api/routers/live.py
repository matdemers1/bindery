"""The WebSocket every screen listens on (T-8.13).

One socket per browser tab, carrying change hints for the libraries that tab's
user can see. Screens react to a hint by refetching what they display, so the
server stays the single definition of every screen's data.
"""

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import events
from api.auth.cookies import ACCESS_COOKIE
from api.auth.tokens import TokenError, decode_access_token
from api.db import repository
from api.db.models import AppUser
from api.db.session import SessionFactory

log = logging.getLogger("bindery.live")

router = APIRouter(tags=["live"])

# Long enough not to be chatty, short enough that a connection broken by a
# sleeping laptop or a proxy is noticed rather than assumed alive.
HEARTBEAT_SECONDS = 25.0


async def _authenticate(websocket: WebSocket, session: AsyncSession) -> AppUser | None:
    """Same cookie as every other request. No second auth scheme for sockets."""
    token = websocket.cookies.get(ACCESS_COOKIE)
    if not token:
        return None
    try:
        user_id = decode_access_token(token)
    except TokenError:
        return None
    user = await session.get(AppUser, user_id)
    return user if user is not None and user.is_active else None


@router.websocket("/live")
async def live(websocket: WebSocket) -> None:
    """Push change hints until the client goes away."""
    async with SessionFactory() as session:
        user = await _authenticate(websocket, session)
        if user is None:
            # Closed rather than accepted-then-closed: an unauthenticated socket
            # should never be established at all.
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        visible = {str(lid) for lid in await repository.visible_library_ids(session, user.id)}

    await websocket.accept()
    queue = events.broadcaster.subscribe()
    log.info("live connection opened (%s subscribers)", events.broadcaster.subscriber_count)

    try:
        # Told immediately whether pushes are actually working, so the client can
        # decide to fall back rather than sit there believing it is up to date.
        await websocket.send_text(
            json.dumps(
                {
                    "topics": ["connected"],
                    "detail": {"pushing": events.broadcaster.connected},
                }
            )
        )

        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                # A write is the only reliable way to notice a dead peer.
                await websocket.send_text('{"topics":["ping"]}')
                continue

            # The boundary applies here exactly as it does to every read: a hint
            # naming a library you are not in is not yours, and a source file id
            # is enough to tell you something exists.
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            library_id = event.get("library_id")
            if library_id is not None and library_id not in visible:
                continue

            await websocket.send_text(payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.info("live connection ended", exc_info=True)
    finally:
        events.broadcaster.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await websocket.close()
