"""Pipeline worker: claim, dispatch, record.

Concurrency is sized to the host (3-4 OCR slots on the 16 GB / Ryzen box). Each
slot is an independent claim loop, so a slow 100-page bundle in one slot does not
stall the others.

Shutdown is graceful: on SIGTERM the loops stop claiming, in-flight work is given
a grace period to finish, and anything still running has its claim released so a
restarting worker picks it up immediately instead of waiting out the lease.
"""

import asyncio
import contextlib
import logging
import os
import signal
import uuid

from api import queue
from api.config import get_settings
from api.db.session import SessionFactory, engine
from worker.ingest.watched_folder import watch_inbox
from worker.stages import STAGES

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("bindery.worker")

IDLE_POLL_SECONDS = 2.0
RECLAIM_INTERVAL_SECONDS = 60.0
SHUTDOWN_GRACE_SECONDS = 20.0


class UnknownStageError(RuntimeError):
    """A job names a stage this build does not implement."""


async def _run_one(job: queue.ClaimedJob) -> None:
    """Execute a claimed job in its own session, then record the outcome.

    The outcome is written in a *separate* session from the stage's own work, so
    a stage that poisons its transaction can still be marked failed.
    """
    try:
        stage_fn = STAGES.get(job.stage)
        if stage_fn is None:
            raise UnknownStageError(f"no implementation for stage {job.stage.value}")
        async with SessionFactory() as session:
            await stage_fn(session, job)
            await session.commit()
    except Exception as exc:  # every failure is recorded; none escape this loop
        log.exception("job %s (%s) failed", job.id, job.stage.value)
        async with SessionFactory() as session:
            state = await queue.fail(session, job.id, job.attempts, repr(exc))
            await session.commit()
        if state is queue.JobState.DEAD_LETTER:
            log.error(
                "job %s (%s) dead-lettered after %s attempts",
                job.id, job.stage.value, job.attempts,
            )
        return

    async with SessionFactory() as session:
        await queue.succeed(session, job.id)
        await session.commit()
    log.info("job %s (%s) succeeded", job.id, job.stage.value)


async def _slot(worker_id: str, stopping: asyncio.Event, in_flight: set[uuid.UUID]) -> None:
    """One claim loop. Runs until `stopping` is set and its current job is done.

    Claiming is wrapped because a slot that dies takes a third of the pipeline
    with it and *looks* fine from outside — the container stays up, the health
    check passes, and jobs simply stop being picked up. That is precisely the
    silent failure invariant 8 exists to prevent, so a failure here is logged
    loudly and the loop backs off rather than exiting.
    """
    stages = list(STAGES)
    failures = 0

    while not stopping.is_set():
        try:
            async with SessionFactory() as session:
                job = await queue.claim(session, stages, worker_id)
                await session.commit()
            failures = 0
        except Exception:
            failures += 1
            # Exponential up to a minute: a database that is down or a schema
            # that has moved under us should not be hammered.
            backoff = min(IDLE_POLL_SECONDS * 2**failures, 60.0)
            log.exception(
                "slot could not claim a job (failure %s); retrying in %.0fs",
                failures, backoff,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=backoff)
            continue

        if job is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=IDLE_POLL_SECONDS)
            continue

        in_flight.add(job.id)
        try:
            await _run_one(job)
        finally:
            in_flight.discard(job.id)


async def _reclaimer(stopping: asyncio.Event) -> None:
    """Return jobs whose worker died mid-run. This is what makes a kill -9 safe."""
    while not stopping.is_set():
        try:
            async with SessionFactory() as session:
                reclaimed = await queue.reclaim_stale(session)
                await session.commit()
            if reclaimed:
                log.warning("reclaimed %s job(s) from expired leases", reclaimed)
        except Exception:
            log.exception("reclaim pass failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=RECLAIM_INTERVAL_SECONDS)


def _report_unexpected_exit(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.critical("worker task %s died: %r", task.get_name(), error)
    else:
        log.warning("worker task %s exited", task.get_name())


async def main() -> None:
    settings = get_settings()
    worker_id = f"{os.uname().nodename}:{os.getpid()}"
    slots = max(settings.worker_concurrency, 1)
    log.info(
        "worker %s starting; slots=%s stages=%s",
        worker_id, slots, ",".join(stage.value for stage in STAGES),
    )

    stopping = asyncio.Event()
    in_flight: set[uuid.UUID] = set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    tasks = [
        asyncio.create_task(_slot(worker_id, stopping, in_flight), name=f"slot-{n}")
        for n in range(slots)
    ]
    tasks.append(asyncio.create_task(_reclaimer(stopping), name="reclaimer"))
    tasks.append(asyncio.create_task(watch_inbox(stopping), name="watched-folder"))

    # Last line of defence: if a task exits despite the guards above, say so
    # rather than letting the worker sit there looking healthy.
    for task in tasks:
        task.add_done_callback(_report_unexpected_exit)

    await stopping.wait()
    log.info("shutdown requested; finishing in-flight work")

    _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    # Anything still held is handed straight back, so a restart picks it up now
    # rather than after the lease expires.
    if in_flight:
        log.warning("releasing %s claim(s) that did not finish in time", len(in_flight))
        async with SessionFactory() as session:
            for job_id in list(in_flight):
                await queue.release(session, job_id)
            await session.commit()

    log.info("worker stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        with contextlib.suppress(RuntimeError):
            asyncio.run(engine.dispose())
