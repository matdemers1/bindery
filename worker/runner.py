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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa

from api import (
    eventlog,
    events,
    health_panel,
    notify,
    offsite,
    offsite_runs,
    queue,
    settings_store,
    version,
)
from api.config import get_settings
from api.db.enums import JobStage
from api.db.models import Document, OffsiteRun, SourceFile
from api.db.session import SessionFactory, engine
from api.export import integrity
from worker import convert
from worker.ai.provider import ProviderRefusedError, ProviderUnavailableError
from worker.ingest.watched_folder import watch_inbox
from worker.stages import STAGES
from worker.stages import normalize as normalize_stage

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
# Everything logged from here on is also written to `event_log`, so a failure is
# explainable from the screen you noticed it on rather than from the host's
# terminal scrollback.
eventlog.install()
log = logging.getLogger("bindery.worker")

IDLE_POLL_SECONDS = 2.0
RECLAIM_INTERVAL_SECONDS = 60.0
# Five minutes: a stall is defined as fifteen minutes of queued-but-idle, so
# checking more often than this would only find the same stall sooner than it
# is a stall.
HEALTH_INTERVAL_SECONDS = 300.0
# Replication is checked often and runs rarely. The check is two indexed
# queries; the run happens when the cadence in `offsite_runs` says it is due,
# which is at most twice a day.
OFFSITE_INTERVAL_SECONDS = 600.0
# A `running` row held longer than this belongs to a worker that is gone. It
# blocks every future run — the partial unique index sees to that — so an
# abandoned row is an outage, not untidiness.
OFFSITE_LEASE = timedelta(hours=6)
SHUTDOWN_GRACE_SECONDS = 20.0

# Liveness for the container healthcheck (see infra/docker-compose.yml and
# infra/zimaos/bindery.zimaos.yaml). Deliberately container-local: the check
# runs inside this container, and writing it under /data would put a
# per-15-second churn file in the archive.
#
# The point is to distinguish a worker that is *idle* from one that is *stuck*,
# which from outside look identical — `Up`, no logs, nothing being claimed. This
# is written from a task on the same event loop the pipeline runs on, so a loop
# that has stopped turning stops refreshing it, and the container goes unhealthy
# instead of sitting there looking fine (invariant 8).
HEARTBEAT_PATH = Path(os.environ.get("WORKER_HEARTBEAT_PATH", "/tmp/bindery-worker.heartbeat"))
HEARTBEAT_INTERVAL_SECONDS = 15.0
# The manifests call the heartbeat stale at 120s — eight missed writes, and then
# three failing checks on top of that before the container is marked unhealthy.
# Generous on purpose: the heavy work is offloaded (`asyncio.to_thread`,
# `create_subprocess_exec`), but a GIL-bound stretch inside a thread can still
# starve the loop for seconds, and a healthcheck that flaps is one that gets
# switched off. Keep this constant well under the manifests' threshold; a test
# asserts the relationship.
HEARTBEAT_STALE_SECONDS = 120.0


class _NotConfigured(Exception):
    """No AWS settings yet. Not an error, and not worth a log line every pass."""


class _NothingDue(Exception):
    """The cadence says no, or another worker got there first."""


class UnknownStageError(RuntimeError):
    """A job names a stage this build does not implement."""


async def _library_of(job: queue.ClaimedJob) -> uuid.UUID | None:
    """Which library this job's work belongs to.

    Looked up so that every log line the stage writes carries it: log messages
    routinely contain filenames, and the boundary that governs a document has to
    govern its diagnostics too.
    """
    try:
        async with SessionFactory() as session:
            if job.source_file_id:
                return await session.scalar(
                    sa.select(SourceFile.library_id).where(SourceFile.id == job.source_file_id)
                )
            if job.document_id:
                return await session.scalar(
                    sa.select(Document.library_id).where(Document.id == job.document_id)
                )
    except Exception:
        # Never let a diagnostic lookup stop real work.
        log.debug("could not resolve library for job %s", job.id)
    return None



async def _announce(session, job: queue.ClaimedJob, library_id, state: str) -> None:
    """Tell whoever is watching that this job moved.

    Emitted from the same transaction that records the outcome, so it is
    delivered on commit and discarded on rollback — a screen can never be told
    about a state change that did not happen.
    """
    topics = [events.Topic.JOBS, events.Topic.LOGS]
    if job.source_file_id:
        topics.append(events.Topic.FILES)
    if job.stage in (JobStage.CLASSIFY, JobStage.RULES, JobStage.SEGMENT):
        # These are the stages that put things in front of a person.
        topics += [events.Topic.REVIEW, events.Topic.DOCUMENTS]
    await events.publish(
        session,
        topics,
        library_id=library_id,
        source_file_id=job.source_file_id,
        stage=job.stage.value,
        state=state,
    )


async def _run_one(job: queue.ClaimedJob) -> None:
    """Execute a claimed job in its own session, then record the outcome.

    The outcome is written in a *separate* session from the stage's own work, so
    a stage that poisons its transaction can still be marked failed.
    """
    # Bound once, here, so every line any stage writes — including lines from
    # code that has never heard of the event log — is attributable to this job,
    # this file and this library.
    library_id = await _library_of(job)
    with eventlog.bind(
        job_id=job.id,
        stage=job.stage.value,
        source_file_id=job.source_file_id,
        document_id=job.document_id,
        library_id=library_id,
        attempt=job.attempts,
    ):
        try:
            stage_fn = STAGES.get(job.stage)
            if stage_fn is None:
                raise UnknownStageError(f"no implementation for stage {job.stage.value}")
            log.info("%s started", job.stage.value)
            async with SessionFactory() as session:
                await stage_fn(session, job)
                await session.commit()
        except Exception as exc:  # every failure is recorded; none escape this loop
            # Any stage may declare a failure permanent. This was an isinstance
            # check against one module's exception, which meant a second stage
            # needing the same thing had to import `normalize` to say so.
            permanent = isinstance(
                exc, normalize_stage.PermanentFailure | ProviderRefusedError
            )
            # An unavailable provider is not a failed job. `ProviderUnavailable`
            # has always been documented as "retried with backoff indefinitely",
            # and was nonetheless being counted against MAX_ATTEMPTS like any
            # other error — so an outage, or an exhausted credit balance, would
            # dead-letter everything waiting on it within about four minutes.
            waiting = isinstance(exc, ProviderUnavailableError)
            async with SessionFactory() as session:
                if waiting:
                    state = await queue.hold(session, job.id, job.attempts, repr(exc))
                else:
                    state = await queue.fail(
                        session, job.id, job.attempts, repr(exc), permanent=permanent
                    )
                await _announce(session, job, library_id, state.value)
                await session.commit()

            # One line per failure, at the level the *outcome* deserves. Logging
            # an attempt that is about to be retried at ERROR fills the error
            # filter with things that then succeed, and an error filter you
            # learn to ignore is the same as not having one. The traceback is
            # attached either way — it is the thing worth having.
            if waiting:
                log.warning(
                    "%s is waiting on the AI provider and will retry without "
                    "counting an attempt: %s",
                    job.stage.value, exc,
                )
            elif state is queue.JobState.DEAD_LETTER:
                log.error(
                    "gave up on %s after %s attempts — it will not retry on its own: %s",
                    job.stage.value, job.attempts, exc, exc_info=exc,
                )
            else:
                log.warning(
                    "%s failed (attempt %s of %s), retrying shortly: %s",
                    job.stage.value, job.attempts, queue.MAX_ATTEMPTS, exc, exc_info=exc,
                )
            return

        async with SessionFactory() as session:
            await queue.succeed(session, job.id)
            await _announce(session, job, library_id, "succeeded")
            await session.commit()
        log.info("%s finished", job.stage.value)


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


async def _heartbeat(stopping: asyncio.Event) -> None:
    """Say, to anything outside this process, that the event loop is still turning.

    The container healthcheck reads the file's age and nothing else — no
    database, no HTTP, no import of this package — so it stays a few
    milliseconds of a bare interpreter and cannot itself wedge on the thing it
    is checking.

    Written inline rather than through `asyncio.to_thread`: a handful of bytes
    to a container-local path, and putting it on a worker thread would mean the
    file kept being stamped by that thread's scheduler for a while after the
    loop it is meant to attest to had stopped.
    """
    while not stopping.is_set():
        try:
            HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_PATH.write_text(f"{datetime.now(UTC).isoformat()}\n")
        except OSError:
            # Never fatal. A worker that cannot write /tmp is a worker that will
            # be restarted by the healthcheck going stale, which is the correct
            # outcome; crashing here would only make it louder and less useful.
            log.exception("could not write the liveness heartbeat to %s", HEARTBEAT_PATH)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)


async def _health_monitor(stopping: asyncio.Event) -> None:
    """Notice a stopped pipeline and say so out loud (REQ-110).

    Runs in the worker rather than the api because the condition it watches for
    is *the worker not working*, and a check that lives in the thing being
    checked is not much of a check. It is still not perfect — a worker that dies
    entirely takes this with it — which is why the api serves the same panel on
    demand and the container healthcheck reports a worker whose loop has stopped
    turning as `unhealthy` (`_heartbeat`, above).

    What that healthcheck does *not* do is restart anything: a plain Docker
    Engine healthcheck marks the container and stops there, and
    `restart: unless-stopped` acts on process exit only. `docs/zimaos-deploy.md`
    says how to make `unhealthy` actionable on the host. Do not write a comment
    here claiming an automatic restart until one exists — the previous version
    of this docstring did, and the mechanism it named had never been deployed.
    """
    notifier: notify.Notifier | None = None
    while not stopping.is_set():
        try:
            async with SessionFactory() as session:
                panel = await health_panel.collect(session)
                webhook = await settings_store.get(session, settings_store.NOTIFY_WEBHOOK_URL)

            # Rebuilt when the webhook changes, so editing it in Settings takes
            # effect without a restart; otherwise kept, because the cooldown
            # state lives on it and a fresh notifier would notify every pass.
            if notifier is None or notifier.webhook_url != (webhook or "").strip():
                notifier = notify.Notifier(webhook)

            for delivery in notifier.dispatch(panel.alerts):
                if delivery.sent:
                    log.warning("notified: %s", delivery.code)
                elif delivery.reason not in ("within cooldown", "no webhook configured"):
                    log.error("could not notify %s: %s", delivery.code, delivery.reason)

            for alert in panel.alerts:
                log.log(
                    logging.ERROR if alert.severity == "critical" else logging.WARNING,
                    "health: %s", alert.message,
                )
            # Piggy-backed on the health pass rather than given its own timer:
            # the two answer the same question — is the worker alive, and which
            # worker is it (REQ-153).
            async with SessionFactory() as session:
                await version.announce(session, "worker")
                await session.commit()
        except Exception:
            log.exception("health pass failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=HEALTH_INTERVAL_SECONDS)


def _report_unexpected_exit(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.critical("worker task %s died: %r", task.get_name(), error)
    else:
        log.warning("worker task %s exited", task.get_name())



async def _offsite_replication(stopping: asyncio.Event) -> None:
    """Get a copy out of the building, on a cadence, without being asked.

    In the worker rather than the api for the same reason the health monitor is:
    it is long-running work that must survive the request that would otherwise
    have started it. A 300 MB upload inside a request handler holds a connection
    open for minutes and dies with it.

    Deliberately **not** a `JobStage`. A global job has a null idempotency key,
    and `enqueue` is `on_conflict_do_nothing` — so the first run would insert and
    every run after it would silently no-op forever, success or failure alike.
    The queue would look perfectly healthy while nothing left the building. See
    ADR-010.
    """
    while not stopping.is_set():
        try:
            async with SessionFactory() as session:
                # Configuration first, and deliberately so. This was the other
                # way round until the loop was watched on a real stack: a worker
                # that came up before its migration was applied — which REQ-114
                # explicitly permits, since migrations are never run on boot —
                # wrote to `offsite_run` before discovering it had nothing to
                # do, and logged an UndefinedTableError stack trace every ten
                # minutes. Reading a table that has existed since Phase 0 costs
                # nothing and cannot fail that way.
                config = await offsite.config_from_settings(session)
                if not config.complete:
                    # Not an error and not worth a log line every ten minutes.
                    # The Trust screen says "not configured", which is where
                    # that belongs.
                    raise _NotConfigured

                released = await offsite_runs.release_stale(
                    session, older_than=OFFSITE_LEASE
                )
                if released:
                    log.warning(
                        "released %s abandoned replication run(s) — a worker died "
                        "mid-run and was blocking every future one", released,
                    )

                due = await offsite_runs.what_is_due(session)
                if due is None:
                    raise _NothingDue

                run = await offsite_runs.begin(session, due)
                if run is None:
                    raise _NothingDue

            # A separate session for the run itself: the upload can take
            # minutes, and holding the connection that claimed the run open for
            # all of it would tie up a pool slot for no reason.
            async with SessionFactory() as session:
                run = await session.get(OffsiteRun, run.id)
                try:
                    # Re-read rather than carrying `config` across sessions: a
                    # credential rotated between the claim and the run should
                    # take effect, not fail with the one it replaced.
                    config = await offsite.config_from_settings(session)
                    # Building a boto3 client loads botocore's service model
                    # off disk and resolves credentials. Small, but blocking,
                    # and this loop is shared with all three OCR slots.
                    client = await asyncio.to_thread(offsite.make_client, config)
                    result = await offsite.replicate(
                        session,
                        config,
                        client,
                        kind=offsite.Kind(run.kind),
                        blob_root=get_settings().blob_root,
                        integrity_report=await integrity.check(session),
                    )
                except Exception as error:
                    # Whatever went wrong, the row must not be left `running`:
                    # it holds the partial unique index and would block every
                    # future run until a human noticed.
                    log.exception("replication run failed")
                    await offsite_runs.abandon(session, run, f"{type(error).__name__}: {error}")
                else:
                    await offsite_runs.finish(session, run, result)
                    if result.ok:
                        log.info("offsite replication: %s", result.detail)
                    else:
                        # The row is the record and the health panel raises the
                        # alert (T-13.8). A second notification path here would
                        # be a second thing to keep in step.
                        log.error("offsite replication failed: %s", result.detail)
        except (_NotConfigured, _NothingDue):
            pass
        except Exception:
            log.exception("offsite replication pass failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=OFFSITE_INTERVAL_SECONDS)


async def main() -> None:
    settings = get_settings()
    worker_id = f"{os.uname().nodename}:{os.getpid()}"
    slots = max(settings.worker_concurrency, 1)
    build = version.build_of_this_process()
    log.info(
        "worker %s starting at %s; slots=%s stages=%s",
        worker_id, build.short, slots, ",".join(stage.value for stage in STAGES),
    )
    # Announced before any work is claimed, so a worker that dies during its
    # first job has still said which build it was.
    #
    # Best-effort, and that is the whole point of the try. Migrations are
    # applied explicitly and never on boot (REQ-114), and the deploy sequence is
    # `docker compose up -d` *then* `alembic upgrade head` — so between those two
    # commands the worker is running against a database that does not yet have
    # the tables this release expects. Crashing there makes the documented
    # deploy sequence broken, and it crashes on the one statement whose only job
    # is telemetry.
    #
    # Found in CI, where the gap is seconds rather than the moments a human
    # takes: the worker died on `relation "service_heartbeat" does not exist`,
    # four jobs sat queued forever, and the browser searched an archive nothing
    # was processing. It survived in production only because that table has
    # existed since Phase 6.
    #
    # The drift itself is not swallowed: `api/version.py` compares the applied
    # revision against the code's head and the UI says "migration pending",
    # which is the thing actually designed to report this.
    async with SessionFactory() as session:
        try:
            await version.announce(session, "worker")
            await session.commit()
        except Exception:
            log.warning(
                "could not record this worker's build — the database is probably "
                "behind this release. Apply migrations: `alembic upgrade head`. "
                "Carrying on; the schema check will report the drift.",
                exc_info=True,
            )
            await session.rollback()

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
    tasks.append(asyncio.create_task(_health_monitor(stopping), name="health-monitor"))
    # Before anything else that could be slow: the healthcheck's start_period is
    # generous, but the first stamp should not be waiting behind a boot.
    tasks.append(asyncio.create_task(_heartbeat(stopping), name="heartbeat"))
    tasks.append(
        asyncio.create_task(_offsite_replication(stopping), name="offsite-replication")
    )
    tasks.append(
        asyncio.create_task(
            eventlog.drain_forever(stopping, SessionFactory), name="log-drain"
        )
    )
    # Fire-and-forget: LibreOffice's first start builds a profile and takes
    # several seconds. Doing it now means the first office document someone
    # adds is not the one that waits for it. Never blocks boot.
    tasks.append(asyncio.create_task(convert.warm_up(), name="converter-warmup"))

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
