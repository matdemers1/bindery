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

from api import queue
from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType, JobState
from api.db.models import AppUser, Job, SourceFile
from api.db.session import get_session
from api.schemas import JobOut, PipelineStatusOut, StageCount

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

    counts = (
        await session.execute(
            sa.select(Job.stage, Job.state, sa.func.count())
            .join(SourceFile, SourceFile.id == Job.source_file_id)
            .where(SourceFile.library_id.in_(library_ids))
            .group_by(Job.stage, Job.state)
            .order_by(Job.stage, Job.state)
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
