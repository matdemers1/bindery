"""Postgres-backed job queue (ADR-002).

`SELECT … FOR UPDATE SKIP LOCKED`. No Redis, no broker. The `job` table is the
observability surface (REQ-108): "what is stuck and why" is a SQL query, and the
health panel reads it directly.

Lives in `api/` rather than `worker/` because **both** processes need it: the
upload endpoint enqueues, the worker claims. `api/` is the only package both
images carry — see the layering rule in CLAUDE.md.

Three properties this module owns:

- **Claimed exactly once.** SKIP LOCKED plus a row-level lock inside the claiming
  transaction. Two workers racing for one job produce one winner and one `None`.
- **Never silently dropped** (invariant 8). A job that keeps failing lands in
  `dead_letter`, which is a visible state, not a deletion.
- **Recoverable.** A worker killed mid-job leaves a `running` row with a stale
  `locked_at`; `reclaim_stale` puts it back on the queue.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.enums import JobStage, JobState
from api.db.models import Job

# A failing job backs off as 1, 2, 4, 8 … minutes, capped. Long enough that a
# flapping dependency is not hammered, short enough that a transient failure
# clears on its own before anyone looks.
BASE_BACKOFF = timedelta(minutes=1)
MAX_BACKOFF = timedelta(hours=1)
MAX_ATTEMPTS = 5

# How long a claim is honoured before another worker may take the job. One
# number for every stage meant it had to suit the slowest — OCR on a 300-page
# bundle — and a classify job that takes four seconds was then stranded for
# three quarters of an hour when a deploy restarted the worker under it. That
# happened twice in one evening.
#
# So the lease is per stage, and each is roughly an order of magnitude above
# what that stage actually takes. Too short is worse than too long: a live job
# gets reclaimed and run twice, and while every stage is idempotent (REQ-111),
# the second run of a classify is a second invoice.
DEFAULT_LEASE = timedelta(minutes=45)
STAGE_LEASES: dict[JobStage, timedelta] = {
    JobStage.NORMALIZE: timedelta(minutes=45),   # OCR, deskew, PDF/A on a bundle
    JobStage.PAGE: timedelta(minutes=30),        # one rasterise per page
    JobStage.SEGMENT: timedelta(minutes=15),     # heuristics, plus an LLM pass on hard seams
    JobStage.CLASSIFY: timedelta(minutes=10),    # one API call, with its retries
    JobStage.EMBED: timedelta(minutes=5),        # local and lexical (ADR-007)
    JobStage.RULES: timedelta(minutes=5),        # pure database work
    JobStage.INGEST: timedelta(minutes=15),      # hashing and storing a large blob
    # `file` and `mirror` are deliberately absent from the worker's STAGES, so a
    # job naming one dead-letters rather than quietly succeeding. They are given
    # leases anyway: the alternative is that whoever implements them inherits 45
    # minutes without choosing it, which is the bug this whole map exists for.
    JobStage.FILE: timedelta(minutes=5),
    JobStage.MIRROR: timedelta(minutes=30),
}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ClaimedJob:
    """A claim, flattened so the claiming transaction can close immediately."""

    id: uuid.UUID
    stage: JobStage
    source_file_id: uuid.UUID | None
    document_id: uuid.UUID | None
    prompt_version: str | None
    attempts: int


async def enqueue(
    session: AsyncSession,
    stage: JobStage,
    *,
    source_file_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    prompt_version: str | None = None,
) -> uuid.UUID | None:
    """Add a job unless an identical one already exists.

    The idempotency key is `(source_file_id, document_id, stage, prompt_version)`,
    so enqueueing the same work twice is a no-op rather than a duplicate run.
    Returns the new job id, or None when one already existed.
    """
    statement = (
        insert(Job)
        .values(
            id=uuid.uuid4(),
            stage=stage.value,
            state=JobState.QUEUED.value,
            source_file_id=source_file_id,
            document_id=document_id,
            prompt_version=prompt_version,
            scheduled_for=_now(),
        )
        .on_conflict_do_nothing()
        .returning(Job.id)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def requeue(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Deliberately re-run a job — a retry from the pipeline screen, or a replay.

    Distinct from `enqueue`, which refuses to disturb existing work. This resets
    the attempt count, so a dead-lettered job gets a full budget again.
    """
    await session.execute(
        sa.update(Job)
        .where(Job.id == job_id)
        .values(
            state=JobState.QUEUED.value,
            attempts=0,
            scheduled_for=_now(),
            locked_at=None,
            locked_by=None,
            last_error=None,
            updated_at=_now(),
        )
    )


async def requeue_stage(
    session: AsyncSession,
    stage: JobStage,
    *,
    source_file_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    prompt_version: str | None = None,
) -> bool:
    """Enqueue a stage, or reset an existing job so it runs again.

    `enqueue` deliberately refuses to disturb existing work, which is right for
    the pipeline and wrong for a deliberate replay — a stage that has already
    succeeded once would otherwise never run again.
    """
    if await enqueue(
        session, stage,
        source_file_id=source_file_id, document_id=document_id,
        prompt_version=prompt_version,
    ):
        return True

    result = await session.execute(
        sa.update(Job)
        .where(
            Job.stage == stage.value,
            Job.source_file_id.is_not_distinct_from(source_file_id),
            Job.document_id.is_not_distinct_from(document_id),
            Job.prompt_version.is_not_distinct_from(prompt_version),
            Job.state != JobState.RUNNING.value,
        )
        .values(
            state=JobState.QUEUED.value,
            attempts=0,
            scheduled_for=_now(),
            locked_at=None,
            locked_by=None,
            last_error=None,
            updated_at=_now(),
        )
    )
    return bool(result.rowcount)


async def claim(
    session: AsyncSession, stages: list[JobStage], worker_id: str
) -> ClaimedJob | None:
    """Take the next due job for one of `stages`, or return None.

    The whole point of SKIP LOCKED: a concurrent claimer steps over this row
    rather than blocking on it, so N workers make progress instead of queueing
    behind each other.
    """
    candidate = (
        sa.select(Job.id)
        .where(
            Job.state == JobState.QUEUED.value,
            Job.scheduled_for <= _now(),
            Job.stage.in_([stage.value for stage in stages]),
        )
        .order_by(Job.scheduled_for, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    claimed = (
        await session.execute(
            sa.update(Job)
            .where(Job.id == candidate)
            .values(
                state=JobState.RUNNING.value,
                attempts=Job.attempts + 1,
                locked_at=_now(),
                locked_by=worker_id,
                updated_at=_now(),
            )
            .returning(
                Job.id, Job.stage, Job.source_file_id, Job.document_id,
                Job.prompt_version, Job.attempts,
            )
        )
    ).one_or_none()

    if claimed is None:
        return None
    return ClaimedJob(
        id=claimed.id,
        stage=JobStage(claimed.stage),
        source_file_id=claimed.source_file_id,
        document_id=claimed.document_id,
        prompt_version=claimed.prompt_version,
        attempts=claimed.attempts,
    )


async def succeed(session: AsyncSession, job_id: uuid.UUID) -> None:
    await session.execute(
        sa.update(Job)
        .where(Job.id == job_id)
        .values(
            state=JobState.SUCCEEDED.value,
            locked_at=None,
            locked_by=None,
            last_error=None,
            updated_at=_now(),
        )
    )


async def hold(
    session: AsyncSession, job_id: uuid.UUID, attempts: int, reason: str
) -> JobState:
    """Put a job back because the world was not ready, not because it failed.

    An unreachable provider, a rate limit, an account that cannot be billed:
    none of these are anything the job did, and none of them get better by
    giving up. `fail` would count each one against `MAX_ATTEMPTS` and
    dead-letter the whole queue over a few minutes of outage — which is how a
    billing problem comes to look like a corpus problem.

    So the attempt `claim` consumed is given back, and the reason is recorded on
    the job where the pipeline view will show it. The job is visible and
    waiting, which is the honest description of its state (invariant 8).
    """
    backoff = min(BASE_BACKOFF * (2 ** max(attempts - 1, 0)), MAX_BACKOFF)
    await session.execute(
        sa.update(Job)
        .where(Job.id == job_id)
        .values(
            state=JobState.QUEUED.value,
            attempts=max(attempts - 1, 0),
            last_error=reason[:4000],
            scheduled_for=_now() + backoff,
            locked_at=None,
            locked_by=None,
            updated_at=_now(),
        )
    )
    return JobState.QUEUED


async def fail(
    session: AsyncSession,
    job_id: uuid.UUID,
    attempts: int,
    error: str,
    *,
    permanent: bool = False,
) -> JobState:
    """Record a failure: back off and retry, or dead-letter.

    `dead_letter` is a visible terminal state a human is expected to look at —
    the queue never drops work on the floor (invariant 8).
    """
    # Some failures cannot be retried into success: a 10x5 pixel image will
    # never contain a document, and a dynamic XFA form will never be readable
    # by anything but Adobe. Retrying those five times over half an hour holds
    # a worker slot to reach the same answer, and buries the real message under
    # four identical ones.
    exhausted = permanent or attempts >= MAX_ATTEMPTS
    state = JobState.DEAD_LETTER if exhausted else JobState.FAILED
    backoff = min(BASE_BACKOFF * (2 ** max(attempts - 1, 0)), MAX_BACKOFF)

    await session.execute(
        sa.update(Job)
        .where(Job.id == job_id)
        .values(
            # A dead-lettered job is not rescheduled; a failed one goes back on
            # the queue once its backoff elapses.
            state=(JobState.DEAD_LETTER if exhausted else JobState.QUEUED).value,
            last_error=error[:4000],
            scheduled_for=_now() + backoff,
            locked_at=None,
            locked_by=None,
            updated_at=_now(),
        )
    )
    return state


async def release(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Hand a claim back untouched — used on graceful shutdown.

    The attempt is not charged against the job: the worker stopping is not the
    job's fault.
    """
    await session.execute(
        sa.update(Job)
        .where(Job.id == job_id, Job.state == JobState.RUNNING.value)
        .values(
            state=JobState.QUEUED.value,
            attempts=sa.func.greatest(Job.attempts - 1, 0),
            locked_at=None,
            locked_by=None,
            updated_at=_now(),
        )
    )


async def reclaim_stale(
    session: AsyncSession, lease: timedelta | None = None
) -> int:
    """Return jobs whose worker died mid-run to the queue.

    A killed container cannot release its own claims, so the lease is what makes
    the queue self-healing. The attempt already charged stands, so a job that
    reliably kills its worker still reaches dead_letter instead of looping.

    Each stage has its own lease (`STAGE_LEASES`), because one number for all of
    them has to suit the slowest and therefore strands the fastest. Passing
    `lease` overrides every stage, which is for tests.
    """
    if lease is not None:
        expired = Job.locked_at < _now() - lease
    else:
        expired = sa.case(
            *[
                (Job.stage == stage.value, Job.locked_at < _now() - stage_lease)
                for stage, stage_lease in STAGE_LEASES.items()
            ],
            else_=Job.locked_at < _now() - DEFAULT_LEASE,
        )

    result = await session.execute(
        sa.update(Job)
        .where(
            Job.state == JobState.RUNNING.value,
            expired,
        )
        .values(
            state=JobState.QUEUED.value,
            scheduled_for=_now(),
            locked_at=None,
            locked_by=None,
            last_error="worker lease expired; reclaimed",
            updated_at=_now(),
        )
    )
    return result.rowcount or 0
