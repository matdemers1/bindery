"""Real-time change notification (T-8.13).

Every screen used to discover change by asking again on a timer. That is wrong
in both directions at once: it hammers the server while nothing is happening,
and it *still* shows stale data for up to a whole interval after something does
— which is why accepting a document left the sidebar badge sitting there
claiming work that was already done.

So changes are pushed. The transport is Postgres `LISTEN`/`NOTIFY`, for one
decisive reason: the worker is a separate container from the api, so a
notification has to cross a process boundary, and the database is the only thing
both of them already talk to. Redis would be a second piece of infrastructure to
run, back up and lose.

Two properties fall out of using the database for this, and both are wanted:

**A notification cannot outrun its transaction.** `pg_notify` inside a
transaction is delivered on commit and discarded on rollback, so it is
impossible to announce a change that then did not happen.

**Nothing is delivered twice or stored.** These are hints that something moved,
not a queue. The durable record of what happened is the archive itself; if a
client misses a hint it refetches on its next interaction and is correct again.

The payload deliberately carries almost nothing — a topic and a library. Clients
refetch what they actually display. Pushing full state would mean maintaining a
replication protocol and a second definition of every screen's data, and the
first time the two disagreed the screen would be quietly wrong.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("bindery.events")

CHANNEL = "bindery_events"

# Postgres refuses a NOTIFY payload over 8000 bytes. Nothing here comes close,
# and the cap is a reason to keep it that way.
MAX_PAYLOAD = 7000


class Topic:
    """What changed, coarsely. Clients subscribe to these."""

    FILES = "files"        # a source file moved through the pipeline
    JOBS = "jobs"          # a job succeeded, failed or gave up
    REVIEW = "review"      # the review queue changed
    DOCUMENTS = "documents"  # a document was filed, edited or superseded
    LOGS = "logs"          # new diagnostics
    SETTINGS = "settings"  # configuration changed


@dataclass
class Event:
    topics: list[str]
    library_id: uuid.UUID | None = None
    source_file_id: uuid.UUID | None = None
    detail: dict = field(default_factory=dict)

    def as_json(self) -> str:
        return json.dumps(
            {
                "topics": self.topics,
                "library_id": str(self.library_id) if self.library_id else None,
                "source_file_id": str(self.source_file_id) if self.source_file_id else None,
                "detail": self.detail,
            },
            separators=(",", ":"),
        )


async def publish(
    session: AsyncSession,
    topics: str | Iterable[str],
    *,
    library_id: uuid.UUID | None = None,
    source_file_id: uuid.UUID | None = None,
    **detail,
) -> None:
    """Announce a change, on commit.

    Never raises. A screen that updates a second late is a much smaller problem
    than a mutation that fails because telling someone about it failed.
    """
    event = Event(
        topics=[topics] if isinstance(topics, str) else list(topics),
        library_id=library_id,
        source_file_id=source_file_id,
        detail=detail,
    )
    payload = event.as_json()
    if len(payload) > MAX_PAYLOAD:
        # Cannot happen with the current callers, and if it ever does the topic
        # alone is still useful — the client refetches either way.
        event.detail = {}
        payload = event.as_json()
    try:
        await session.execute(
            sa.text("SELECT pg_notify(:channel, :payload)"),
            {"channel": CHANNEL, "payload": payload},
        )
    except Exception:
        log.warning("could not queue a change notification", exc_info=True)


# --------------------------------------------------------------------------
# The api side: one listener, fanned out to connected clients
# --------------------------------------------------------------------------


class Broadcaster:
    """Fans database notifications out to whoever is currently connected.

    One `LISTEN` connection for the whole process, not one per client: the
    database should not care how many browser tabs are open.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._connection = None
        self.connected = False

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def dispatch(self, payload: str) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A client too slow to keep up gets dropped hints, not a stalled
                # server. It refetches on its next interaction regardless.
                log.debug("dropping a notification for a slow subscriber")

    async def run(self, dsn: str, stopping: asyncio.Event) -> None:
        """Hold a LISTEN connection open, reconnecting for as long as we are up.

        Reconnects rather than dies: losing this connection would silently turn
        every screen static, which is precisely the failure the whole feature
        exists to remove.
        """
        import asyncpg

        backoff = 1.0
        while not stopping.is_set():
            try:
                self._connection = await asyncpg.connect(dsn)
                await self._connection.add_listener(
                    CHANNEL, lambda _c, _p, _ch, payload: self.dispatch(payload)
                )
                self.connected = True
                backoff = 1.0
                log.info("listening for change notifications")
                await stopping.wait()
            except Exception as error:
                self.connected = False
                log.warning("notification listener dropped (%s); retrying", error)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self.connected = False
                if self._connection is not None:
                    with contextlib.suppress(Exception):
                        await self._connection.close()
                    self._connection = None


broadcaster = Broadcaster()


def listen_dsn(database_url: str) -> str:
    """A plain libpq DSN for asyncpg, from the SQLAlchemy URL."""
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)
