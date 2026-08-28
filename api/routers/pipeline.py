"""Pipeline status (T-1.10, REQ-070, REQ-108).

> Nothing fails silently.

The `job` table is the observability surface, so this endpoint is a handful of
queries against it rather than a separate tracking system. Every failed document
appears here, and every dead-lettered job can be retried from here.
"""

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import events, queue, reclassify
from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType, JobState
from api.db.models import AppUser, Job
from api.db.session import get_session
from api.schemas import (
    JobOut,
    PendingReviewOut,
    PipelineStatusOut,
    ReclassifyIn,
    ReclassifyResultOut,
    StageCount,
)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

# States a human is expected to act on, newest first.
ATTENTION_STATES = (JobState.DEAD_LETTER, JobState.FAILED)


@router.get("", response_model=PipelineStatusOut)
async def status_(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PipelineStatusOut:
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return PipelineStatusOut(counts=[], attention=[], in_flight=[])

    # Counted through the same reachability rule as the lists below, or the
    # totals disagree with what the screen shows.
    visible = repository.visible_jobs(library_ids).subquery()
    counts = (
        await session.execute(
            sa.select(visible.c.stage, visible.c.state, sa.func.count())
            .group_by(visible.c.stage, visible.c.state)
            .order_by(visible.c.stage, visible.c.state)
        )
    ).all()

    attention = (
        await session.execute(
            repository.visible_jobs(library_ids)
            .where(Job.state.in_([state.value for state in ATTENTION_STATES]))
            .order_by(Job.updated_at.desc())
            .limit(50)
        )
    ).scalars().all()

    in_flight = (
        await session.execute(
            repository.visible_jobs(library_ids)
            .where(Job.state == JobState.RUNNING.value)
            .order_by(Job.locked_at)
            .limit(50)
        )
    ).scalars().all()

    return PipelineStatusOut(
        counts=[
            StageCount(stage=str(stage), state=str(state), count=count)
            for stage, state, count in counts
        ],
        attention=[JobOut.model_validate(job) for job in attention],
        in_flight=[JobOut.model_validate(job) for job in in_flight],
    )


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def retry(
    job_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    """Put a failed or dead-lettered job back on the queue with a fresh budget."""
    library_ids = await repository.writable_library_ids(session, user.id)
    if not library_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no writable libraries")

    job = (
        await session.execute(repository.visible_jobs(library_ids).where(Job.id == job_id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if job.state is JobState.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, "job is currently running")

    before = {"state": job.state.value, "attempts": job.attempts, "last_error": job.last_error}
    await queue.requeue(session, job_id)
    await record(
        session,
        entity_type="job",
        entity_id=job_id,
        action="retry",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        before=before,
        after={"state": JobState.QUEUED.value, "attempts": 0},
    )
    await session.commit()

    await session.refresh(job)
    return JobOut.model_validate(job)


# --------------------------------------------------------------------------
# Re-running AI review (T-8.10)
# --------------------------------------------------------------------------


@router.get("/reclassify/pending", response_model=PendingReviewOut)
async def pending_review(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> PendingReviewOut:
    """What is waiting for AI review, split by why.

    "Never attempted" and "gave up" are reported separately because they are
    different situations wearing the same face: the first is the ordinary
    consequence of adding documents before an API key, and calling it a failure
    would be both alarming and wrong.
    """
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return PendingReviewOut(total=0, reasons=[])
    result = await reclassify.pending(session, library_ids)
    return PendingReviewOut(**result.as_dict())


@router.post("/reclassify", response_model=ReclassifyResultOut)
async def rerun_review(
    body: ReclassifyIn,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> ReclassifyResultOut:
    """Re-run AI review over specific documents, or everything that is waiting.

    A replay, not a repair: the classify job is reset and the ordinary pipeline
    runs. An existing classification is not touched until a new one succeeds, so
    the worst case of pressing this is that nothing changes.
    """
    library_ids = await repository.writable_library_ids(session, user.id)
    if not library_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no writable libraries")

    document_ids = list(body.document_ids)
    if body.all_pending:
        waiting = await reclassify.pending(session, library_ids)
        codes = set(body.reasons) if body.reasons else None
        document_ids += [
            document_id
            for reason in waiting.reasons
            if codes is None or reason.code in codes
            for document_id in reason.document_ids
        ]

    queued = await reclassify.requeue(session, document_ids, library_ids)
    await record(
        session,
        entity_type="pipeline",
        entity_id=user.id,
        action="reclassify_requested",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"queued": queued, "requested": len(set(document_ids))},
    )
    await events.publish(session, [events.Topic.JOBS, events.Topic.REVIEW])
    await session.commit()
    return ReclassifyResultOut(queued=queued, requested=len(set(document_ids)))

