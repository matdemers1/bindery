"""Give the e2e suite the two job states it exists to check.

`scripts/seed-demo.py` produces a healthy archive, which is the right fixture
for screenshots and the wrong one for the pipeline specs: with nothing declined
and nothing dead-lettered they skip, and two tests that always skip are two
tests that have stopped saying anything.

So this adds exactly one of each, on invented files:

- a **declined** job, standing in for the 10x5 pixel image the real archive
  refused — it must be listed and must offer nothing to acknowledge;
- a **dead-lettered** job, which must light the badge and must survive being
  acknowledged, with its error intact.

Piped into the api container rather than run from it: the runtime image ships
`api/` and `alembic/` and deliberately not `scripts/`.

    docker compose exec -T api python - < scripts/seed-e2e-states.py
"""

import asyncio
import os
import uuid

import sqlalchemy as sa

from api.db.enums import IngestSource, JobStage, JobState
from api.db.models import AppUser, Job, Library, Membership, SourceFile
from api.db.session import SessionFactory

EMAIL = os.environ.get("BINDERY_EMAIL", "demo@example.com")

CASES = [
    (
        "laser-cutting-asset.png",
        JobState.DECLINED,
        "PermanentFailure('10x5 pixels is too small to be a document page "
        "(each side must be at least 16px). This is usually a design or "
        "laser-cutting asset rather than a document.')",
    ),
    (
        "scan-that-timed-out.pdf",
        JobState.DEAD_LETTER,
        "TimeoutError('OCR did not finish within 600s')",
    ),
]


async def main() -> None:
    async with SessionFactory() as session:
        user = (
            await session.execute(sa.select(AppUser).where(AppUser.email == EMAIL))
        ).scalar_one_or_none()
        if user is None:
            raise SystemExit(f"no such user: {EMAIL}")

        library_id = (
            await session.execute(
                sa.select(Membership.library_id).where(Membership.user_id == user.id).limit(1)
            )
        ).scalar_one_or_none()
        if library_id is None:
            raise SystemExit(f"{EMAIL} owns no library")
        name = (
            await session.execute(sa.select(Library.name).where(Library.id == library_id))
        ).scalar_one()

        for filename, state, error in CASES:
            existing = (
                await session.execute(
                    sa.select(SourceFile).where(
                        SourceFile.library_id == library_id,
                        SourceFile.original_filename == filename,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"{filename} already there")
                continue

            source = SourceFile(
                library_id=library_id,
                sha256=uuid.uuid4().hex * 2,
                byte_size=64,
                original_filename=filename,
                ingest_source=IngestSource.WEB_UPLOAD,
            )
            session.add(source)
            await session.flush()
            session.add(
                Job(
                    source_file_id=source.id,
                    stage=JobStage.NORMALIZE,
                    state=state,
                    # A refusal is reached on the first look; a dead letter has
                    # spent its whole budget. The screen says both.
                    attempts=1 if state is JobState.DECLINED else 5,
                    last_error=error,
                )
            )
            print(f"{filename} -> {state.value} in {name}")

        # Everything already dead-lettered gets acknowledged, so the one this
        # script creates is the *only* thing outstanding. Without it the badge
        # assertion depends on whatever else the archive happens to be carrying
        # — locally that was 27 classify failures reachable through
        # `document_id` rather than `source_file_id`, which is exactly the kind
        # of thing a fixture exists to remove from the question.
        from datetime import UTC, datetime

        from api.db import repository

        others = (
            await session.execute(
                sa.select(Job).where(
                    Job.state == JobState.DEAD_LETTER,
                    Job.acknowledged_at.is_(None),
                    Job.id.in_(repository.visible_job_ids([library_id])),
                )
            )
        ).scalars().all()
        now = datetime.now(UTC)
        settled = 0
        for job in others:
            source = await session.get(SourceFile, job.source_file_id) if job.source_file_id else None
            if source is not None and source.original_filename == "scan-that-timed-out.pdf":
                continue  # the one the suite is here to acknowledge itself
            job.acknowledged_at = now
            settled += 1
        if settled:
            print(f"acknowledged {settled} pre-existing dead letter(s) to make the state known")

        await session.commit()


asyncio.run(main())
