"""T-1.1 — the queue everything else rides on.

The three properties that matter: claimed exactly once under concurrency, a
killed worker's job comes back, and nothing is ever silently dropped.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from api import queue
from api.db.enums import IngestSource, JobStage, JobState, LibraryKind
from api.db.models import Job, Library, SourceFile
from api.db.session import SessionFactory


@pytest.fixture(autouse=True)
async def empty_queue(session):
    """Start each test with nothing due.

    `claim` deliberately has no way to ask for one specific job — it takes the
    next due one — so tests have to control what is due rather than filter.
    """
    await session.execute(
        sa.update(Job)
        .where(Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]))
        .values(state=JobState.SUCCEEDED.value)
    )
    await session.commit()


async def _source_file(session, sha: str) -> SourceFile:
    library = Library(name=f"Q {sha[:6]}", kind=LibraryKind.PERSONAL)
    session.add(library)
    await session.flush()
    source_file = SourceFile(
        library_id=library.id,
        sha256=sha,
        byte_size=1,
        ingest_source=IngestSource.WEB_UPLOAD,
    )
    session.add(source_file)
    await session.flush()
    return source_file


async def test_enqueue_is_idempotent(session) -> None:
    source_file = await _source_file(session, "a" * 64)

    first = await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    second = await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()

    assert first is not None
    # The idempotency key already covered this work, so nothing was added.
    assert second is None


async def test_claim_returns_none_when_nothing_is_due(session) -> None:
    source_file = await _source_file(session, "b" * 64)
    await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()

    # A stage nobody enqueued for.
    assert await queue.claim(session, [JobStage.CLASSIFY], "w1") is None


async def test_claim_skips_jobs_scheduled_in_the_future(session) -> None:
    source_file = await _source_file(session, "c" * 64)
    job_id = await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.execute(
        sa.update(Job)
        .where(Job.id == job_id)
        .values(scheduled_for=datetime.now(UTC) + timedelta(hours=1))
    )
    await session.commit()

    assert await queue.claim(session, [JobStage.NORMALIZE], "w1") is None


async def test_a_job_is_claimed_exactly_once_under_concurrency(session) -> None:
    """REQ-111. Two workers, one job — one winner and one None, never two."""
    source_file = await _source_file(session, "d" * 64)
    await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()

    async def claimer(worker_id: str):
        async with SessionFactory() as own:
            claimed = await queue.claim(own, [JobStage.NORMALIZE], worker_id)
            await own.commit()
            return claimed

    results = await asyncio.gather(*(claimer(f"w{n}") for n in range(6)))

    winners = [claimed for claimed in results if claimed is not None]
    assert len(winners) == 1
    assert winners[0].attempts == 1


async def test_failure_backs_off_then_dead_letters(session) -> None:
    source_file = await _source_file(session, uuid.uuid4().hex * 2)
    job_id = await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()

    state = await queue.fail(session, job_id, attempts=1, error="boom")
    await session.commit()
    job = await session.get(Job, job_id)
    await session.refresh(job)

    assert state is JobState.FAILED
    # Back on the queue, but not immediately.
    assert job.state is JobState.QUEUED
    assert job.scheduled_for > datetime.now(UTC)
    assert job.last_error == "boom"

    state = await queue.fail(session, job_id, attempts=queue.MAX_ATTEMPTS, error="boom")
    await session.commit()
    await session.refresh(job)

    # Terminal, visible, and not deleted — nothing fails silently.
    assert state is JobState.DEAD_LETTER
    assert job.state is JobState.DEAD_LETTER


async def test_a_dead_worker_s_job_is_reclaimed(session) -> None:
    """A killed container cannot release its own claim; the lease is the safety net."""
    source_file = await _source_file(session, uuid.uuid4().hex * 2)
    await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()

    claimed = await queue.claim(session, [JobStage.NORMALIZE], "doomed-worker")
    await session.commit()
    assert claimed is not None

    # Nothing to reclaim while the lease is live.
    assert await queue.reclaim_stale(session) == 0

    await session.execute(
        sa.update(Job).where(Job.id == claimed.id).values(
            locked_at=datetime.now(UTC) - queue.DEFAULT_LEASE - timedelta(minutes=1)
        )
    )
    await session.commit()

    assert await queue.reclaim_stale(session) == 1
    await session.commit()

    again = await queue.claim(session, [JobStage.NORMALIZE], "fresh-worker")
    await session.commit()
    assert again is not None
    assert again.id == claimed.id
    # The attempt already charged stands, so a job that kills its worker every
    # time still reaches dead_letter instead of looping for ever.
    assert again.attempts == 2


async def test_release_hands_a_claim_back_without_charging_an_attempt(session) -> None:
    source_file = await _source_file(session, "0" * 64)
    await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()

    claimed = await queue.claim(session, [JobStage.NORMALIZE], "shutting-down")
    await session.commit()
    assert claimed is not None and claimed.attempts == 1

    await queue.release(session, claimed.id)
    await session.commit()

    again = await queue.claim(session, [JobStage.NORMALIZE], "restarted")
    await session.commit()
    assert again is not None
    # The worker stopping is not the job's fault.
    assert again.attempts == 1


async def test_requeue_resets_a_dead_lettered_job(session) -> None:
    source_file = await _source_file(session, "1" * 64)
    job_id = await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await queue.fail(session, job_id, attempts=queue.MAX_ATTEMPTS, error="boom")
    await session.commit()

    await queue.requeue(session, job_id)
    await session.commit()

    job = await session.get(Job, job_id)
    await session.refresh(job)
    assert job.state is JobState.QUEUED
    assert job.attempts == 0
    assert job.last_error is None


async def test_a_permanent_failure_is_declined_not_dead_lettered(session) -> None:
    """Some inputs cannot be retried into success — and are not failures.

    A 10x5 pixel image will never hold a document and a dynamic XFA form will
    never be readable outside Adobe. Spending five attempts over half an hour
    to reach that conclusion holds a worker slot and buries the one useful
    message under four identical ones.

    It lands in `declined` rather than `dead_letter` because the two mean
    opposite things: `dead_letter` is work that never reached an answer, and
    `declined` is the pipeline reaching a correct one about something that is
    not a document. Both are terminal; only the first is a fault.

    This test asserted `DEAD_LETTER` until ADR-011. The distinction existed at
    the point of failure — `permanent=True` is what short-circuits the retry
    budget — and was thrown away one line later, which left a red badge on the
    sidebar that nothing could ever clear.
    """
    source_file = await _source_file(session, uuid.uuid4().hex * 2)
    job_id = await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()
    claimed = await queue.claim(session, [JobStage.NORMALIZE], "worker-1")
    assert claimed is not None

    state = await queue.fail(
        session, job_id, claimed.attempts, "too small to be a document page", permanent=True
    )
    await session.commit()

    assert state is JobState.DECLINED
    job = await session.get(Job, job_id)
    assert job.state is JobState.DECLINED
    assert job.attempts == 1, "it gave up on the first attempt, not the fifth"
    assert job.scheduled_for is not None, "still a row, not a deletion"


async def test_an_ordinary_failure_still_retries(session) -> None:
    """The fast path must not swallow the failures that do recover — a network
    blip on the way to the API is exactly what the retries are for."""
    source_file = await _source_file(session, uuid.uuid4().hex * 2)
    job_id = await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()
    claimed = await queue.claim(session, [JobStage.NORMALIZE], "worker-1")

    state = await queue.fail(session, job_id, claimed.attempts, "connection reset")
    await session.commit()

    assert state is JobState.FAILED
    job = await session.get(Job, job_id)
    assert job.state is JobState.QUEUED, "back on the queue for another go"


async def test_a_held_job_does_not_spend_an_attempt(session) -> None:
    """An unavailable provider is not a failed job.

    `fail` counts every error against MAX_ATTEMPTS, so five minutes of outage
    dead-letters everything waiting on it. The work was never tried; the
    attempt `claim` consumed is given back.
    """
    from api import queue

    source_file = await _source_file(session, "cd" * 32)
    await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()
    claimed = await queue.claim(session, [JobStage.NORMALIZE], "worker-1")
    assert claimed is not None and claimed.attempts == 1

    state = await queue.hold(
        session, claimed.id, claimed.attempts, "ProviderUnavailableError('no credit')"
    )
    await session.commit()

    assert state is queue.JobState.QUEUED
    job = await session.get(Job, claimed.id)
    assert job.attempts == 0, "the attempt is given back"
    assert job.state == queue.JobState.QUEUED.value
    assert "no credit" in job.last_error, "and the reason is visible"
    assert job.locked_by is None


async def test_holding_repeatedly_never_dead_letters(session) -> None:
    """The whole point: an outage must not consume the queue."""
    from api import queue

    source_file = await _source_file(session, "ce" * 32)
    await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await session.commit()

    for _ in range(queue.MAX_ATTEMPTS * 2):
        claimed = await queue.claim(session, [JobStage.NORMALIZE], "worker-1")
        assert claimed is not None, "a held job stays claimable"
        await queue.hold(session, claimed.id, claimed.attempts, "unavailable")
        # Fast-forward past the backoff rather than sleeping through it.
        await session.execute(
            sa.update(Job).where(Job.id == claimed.id).values(scheduled_for=queue._now())
        )
        await session.commit()

    job = await session.get(Job, claimed.id)
    assert job.state == queue.JobState.QUEUED.value


async def test_a_stalled_classify_is_reclaimed_long_before_a_stalled_ocr(
    session,
) -> None:
    """One lease for every stage has to suit the slowest.

    Three classify jobs sat stranded for forty-five minutes after a deploy
    restarted the worker under them, because the lease was sized for OCR on a
    300-page bundle. A classify job takes four seconds.
    """
    from datetime import timedelta

    from api import queue

    source_file = await _source_file(session, "cf" * 32)
    await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await queue.enqueue(session, JobStage.CLASSIFY, document_id=None,
                        source_file_id=source_file.id)
    await session.commit()

    # Both claimed, both abandoned twenty minutes ago.
    for _ in range(2):
        claimed = await queue.claim(
            session, [JobStage.NORMALIZE, JobStage.CLASSIFY], "doomed-worker"
        )
        assert claimed is not None
        await session.execute(
            sa.update(Job)
            .where(Job.id == claimed.id)
            .values(locked_at=queue._now() - timedelta(minutes=20))
        )
    await session.commit()

    reclaimed = await queue.reclaim_stale(session)
    await session.commit()

    assert reclaimed == 1, "the classify job comes back; the OCR job is still working"
    states = dict(
        (row.stage, row.state)
        for row in (
            await session.execute(
                sa.select(Job.stage, Job.state).where(
                    Job.source_file_id == source_file.id
                )
            )
        ).all()
    )
    assert states[JobStage.CLASSIFY.value] == JobState.QUEUED.value
    assert states[JobStage.NORMALIZE.value] == JobState.RUNNING.value


def test_every_stage_has_a_lease() -> None:
    """A stage added without one silently inherits 45 minutes."""
    from api import queue

    assert set(queue.STAGE_LEASES) == set(JobStage), (
        "a new stage needs a lease sized to what it actually takes"
    )


async def test_replaying_resets_the_existing_job_rather_than_adding_one(
    session,
) -> None:
    """Uniqueness includes `prompt_version`, so a replay that did not name one
    used to slip past the constraint and add a *second* job.

    The dead-lettered original stayed exactly where it was, so a document could
    classify successfully and still sit on the failures screen — which is the
    one screen whose whole purpose is that nothing fails silently.
    """
    from api import queue

    source_file = await _source_file(session, "d0" * 32)
    session.add(
        Job(
            source_file_id=source_file.id, stage=JobStage.NORMALIZE,
            state=JobState.DEAD_LETTER, attempts=5, prompt_version="v1",
            last_error="gave up",
        )
    )
    await session.commit()

    assert await queue.requeue_stage(
        session, JobStage.NORMALIZE, source_file_id=source_file.id
    )
    await session.commit()

    jobs = (
        await session.execute(
            sa.select(Job).where(Job.source_file_id == source_file.id)
        )
    ).scalars().all()
    assert len(jobs) == 1, "one job, reset — not two"
    assert jobs[0].state == JobState.QUEUED.value
    assert jobs[0].attempts == 0
    assert jobs[0].last_error is None
    assert jobs[0].prompt_version == "v1", "the version it was recorded under"


async def test_naming_a_prompt_version_still_targets_only_that_one(session) -> None:
    """"Reprocess everything still on v2" (REQ-113) needs the narrow behaviour."""
    from api import queue

    source_file = await _source_file(session, "d1" * 32)
    session.add(
        Job(
            source_file_id=source_file.id, stage=JobStage.NORMALIZE,
            state=JobState.SUCCEEDED, prompt_version="v1",
        )
    )
    await session.commit()

    await queue.requeue_stage(
        session, JobStage.NORMALIZE, source_file_id=source_file.id, prompt_version="v2"
    )
    await session.commit()

    jobs = (
        await session.execute(
            sa.select(Job).where(Job.source_file_id == source_file.id)
        )
    ).scalars().all()
    versions = {job.prompt_version: job.state for job in jobs}
    assert versions["v1"] == JobState.SUCCEEDED.value, "untouched"
    assert versions["v2"] == JobState.QUEUED.value, "a new one for the new version"


async def test_a_first_run_still_enqueues(session) -> None:
    from api import queue

    source_file = await _source_file(session, "d2" * 32)
    await session.commit()

    assert await queue.requeue_stage(
        session, JobStage.NORMALIZE, source_file_id=source_file.id
    )
    await session.commit()

    count = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Job).where(
                Job.source_file_id == source_file.id
            )
        )
    ).scalar_one()
    assert count == 1
