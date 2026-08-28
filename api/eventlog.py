"""Making log output survive the container it was printed in (T-8.11).

Everything here exists to answer one question from the screen where you noticed
the problem: **why did this file not work?**

The design is a `logging.Handler` rather than a bespoke `log_event()` call,
because there were already 75 log statements scattered through the pipeline
saying exactly the right things to the wrong place. A handler adopts all of them
at once, and — more importantly — keeps adopting the ones written next year by
someone who has never read this module.

Three rules, each of which is the whole point of some piece of the code below:

**Logging must never break the thing it is logging.** `emit` cannot raise, and
cannot block. It drops a record onto a bounded in-memory queue and returns.
Every failure past that point is swallowed, counted, and reported through the
same channel rather than propagated.

**A log row must not roll back with the failure it describes.** The drain owns
its own session, so a row recording *why* a transaction failed is not undone by
that transaction failing.

**Context travels with the work, not the call site.** A stage binds the file it
is working on once; every log line inside that stage — including ones written by
library code that has never heard of Bindery — is tagged with it.
"""

import asyncio
import contextlib
import contextvars
import logging
import queue
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger("bindery.eventlog")

# Bounded on purpose. If the drain stalls, the correct failure is to lose the
# oldest diagnostics and say so — not to grow until the worker is killed for
# memory, which would destroy the thing the diagnostics were for.
QUEUE_SIZE = 4096

# DEBUG is for a person watching a terminal. Persisting it would multiply the
# table by an order of magnitude to store lines nobody queries.
PERSIST_FROM = logging.INFO

# `None` rather than `{}`: a mutable default on a ContextVar is shared by every
# context that never sets it, which is a footgun even when nothing mutates it.
_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "bindery_log_context", default=None
)

_records: queue.Queue[dict] = queue.Queue(maxsize=QUEUE_SIZE)
_dropped = 0


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Tag every log line written inside this block.

    Nested binds merge, so a stage can bind the file and an inner helper can add
    the page without either needing to know about the other.
    """
    current = _context.get() or {}
    token = _context.set({**current, **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def context() -> dict[str, Any]:
    return dict(_context.get() or {})


class DatabaseLogHandler(logging.Handler):
    """Copies log records onto the queue. Never raises, never blocks."""

    def emit(self, record: logging.LogRecord) -> None:
        global _dropped
        try:
            if record.levelno < PERSIST_FROM:
                return
            # Bindery's own loggers only: SQLAlchemy and uvicorn at INFO would
            # bury the pipeline's story in their own.
            if not record.name.startswith("bindery"):
                return

            bound = _context.get() or {}
            payload = {
                "level": record.levelname.lower(),
                "logger": record.name,
                "message": record.getMessage()[:8000],
                "detail": self.format_exception(record),
                "library_id": _as_uuid(bound.get("library_id")),
                "source_file_id": _as_uuid(bound.get("source_file_id")),
                "document_id": _as_uuid(bound.get("document_id")),
                "job_id": _as_uuid(bound.get("job_id")),
                "stage": _as_text(bound.get("stage")),
                "context": {
                    key: str(value)
                    for key, value in bound.items()
                    if key not in {
                        "library_id", "source_file_id", "document_id", "job_id", "stage"
                    }
                },
            }
            _records.put_nowait(payload)
        except queue.Full:
            # Drop the newest rather than block a pipeline stage on a log line.
            _dropped += 1
        except Exception:
            # A logging handler that raises takes down the code it was watching.
            _dropped += 1

    @staticmethod
    def format_exception(record: logging.LogRecord) -> str | None:
        if not record.exc_info:
            return None
        import traceback

        return "".join(traceback.format_exception(*record.exc_info))


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _as_text(value: Any) -> str | None:
    return None if value is None else str(value)


def pending() -> int:
    return _records.qsize()


def dropped() -> int:
    return _dropped


async def drain_once(session_factory) -> int:
    """Write whatever is queued. Returns how many rows landed."""
    global _dropped

    batch: list[dict] = []
    while len(batch) < 500:
        try:
            batch.append(_records.get_nowait())
        except queue.Empty:
            break

    lost, _dropped = _dropped, 0
    if lost:
        batch.append({
            "level": "warning",
            "logger": "bindery.eventlog",
            "message": (
                f"{lost} log record(s) were dropped: they arrived faster than they "
                "could be written. The pipeline was not affected."
            ),
            "detail": None, "library_id": None, "source_file_id": None,
            "document_id": None, "job_id": None, "stage": None, "context": {},
        })

    if not batch:
        return 0

    from api.db.models import EventLog

    try:
        async with session_factory() as session:
            session.add_all([EventLog(**row) for row in batch])
            await session.commit()
    except Exception as error:
        # Nothing to do but say so on stdout, which is where these lines used to
        # go anyway. Re-queueing would risk a loop that logs about failing to log.
        logging.getLogger("bindery.eventlog").warning(
            "could not persist %s log record(s): %s", len(batch), error
        )
        return 0
    return len(batch)


async def drain_forever(stopping: asyncio.Event, session_factory, interval: float = 2.0) -> None:
    """Flush the queue on a timer until asked to stop, then flush once more."""
    while not stopping.is_set():
        try:
            await drain_once(session_factory)
        except Exception:
            logging.getLogger("bindery.eventlog").exception("log drain pass failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=interval)
    # A crash is exactly when the last few lines matter most.
    await drain_once(session_factory)


def install() -> DatabaseLogHandler:
    """Attach the handler to the root logger. Idempotent.

    Also pins the `bindery` logger to the level being persisted. Without it,
    whether a line is captured depends on whoever last called `basicConfig` —
    so a module logging perfectly good diagnostics could be silently dropped
    because the root logger happened to be left at WARNING.
    """
    logging.getLogger("bindery").setLevel(min(PERSIST_FROM, logging.INFO))
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, DatabaseLogHandler):
            return existing
    handler = DatabaseLogHandler()
    handler.setLevel(PERSIST_FROM)
    root.addHandler(handler)
    return handler
