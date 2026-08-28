"""Bindery API.

Everything is mounted under /api so nginx can proxy a single prefix and the
Cloudflare Access service-token policy has one path to match (REQ-105).
"""

import asyncio
import contextlib
import logging

from fastapi import FastAPI

from api import eventlog, events
from api.config import get_settings
from api.db.session import SessionFactory
from api.routers import (
    auth,
    documents,
    entities,
    files,
    health,
    household,
    imports,
    library,
    live,
    logs,
    pipeline,
    review,
    rules,
    search,
    segments,
    settings,
    trust,
    upload,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Persist the api's own log output for the same reason the worker does.

    The drain runs here rather than in a request: a log line written while
    answering a request must not be able to slow that request down, and must
    outlive it — the interesting lines are the ones written by requests that
    failed.
    """
    eventlog.install()
    stopping = asyncio.Event()
    drain = asyncio.create_task(
        eventlog.drain_forever(stopping, SessionFactory), name="log-drain"
    )
    # One LISTEN connection for the whole process, fanned out to every open
    # socket. The database should not care how many browser tabs there are.
    listener = asyncio.create_task(
        events.broadcaster.run(events.listen_dsn(get_settings().database_url), stopping),
        name="change-listener",
    )
    try:
        yield
    finally:
        stopping.set()
        for task in (drain, listener):
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=5)


app = FastAPI(
    title="Bindery",
    version="0.1.0",
    description="Self-hosted document archive with page-level retrieval.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(upload.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(library.router, prefix="/api")
app.include_router(household.router, prefix="/api")
app.include_router(imports.router, prefix="/api")
app.include_router(entities.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(segments.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(live.router, prefix="/api")
app.include_router(review.router, prefix="/api")
app.include_router(rules.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(trust.router, prefix="/api")
