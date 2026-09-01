"""Give the e2e suite the two job states it exists to check.

`scripts/seed-demo.py` produces a healthy archive, which is the right fixture
for screenshots and the wrong one for the pipeline specs: with nothing declined
and nothing dead-lettered they skip, and two tests that always skip are two
tests that have stopped saying anything.

So this adds exactly one of each, on invented files:

- a **declined** job, standing in for the 10x5 pixel image the real archive
  refused — it must be listed and must offer nothing to acknowledge;
- a **dead-lettered** job, which must light the badge and must survive being
  acknowledged, with its error intact;
- a document **waiting for review**, so the correction specs (Phase 17) have
  something to correct. Without it the queue is empty on a healthy seeded
  archive and the correct-then-accept test skips — which is the same nothing
  the two above were added to stop.

Piped into the api container rather than run from it: the runtime image ships
`api/` and `alembic/` and deliberately not `scripts/`.

    docker compose exec -T api python - < scripts/seed-e2e-states.py
"""

import asyncio
import os
import uuid

import sqlalchemy as sa

from api.db.enums import IngestSource, JobStage, JobState, ReviewState, SourceFileState
from api.db.models import (
    AppUser,
    Document,
    FieldSource,
    Job,
    Library,
    Membership,
    Page,
    SourceFile,
)
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
                # Establish the state, do not merely notice the row. The suite
                # acknowledges this dead letter, so a second run found it
                # already acknowledged and failed on a fixture rather than on
                # the behaviour. A seed that is not idempotent works exactly
                # once, which is the same as not working.
                job = (
                    await session.execute(
                        sa.select(Job).where(Job.source_file_id == existing.id)
                    )
                ).scalar_one_or_none()
                if job is not None:
                    job.state = state
                    job.acknowledged_at = None
                    job.last_error = error
                print(f"{filename} reset to {state.value}")
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

        await _document_awaiting_review(session, library_id)

        await session.commit()


async def _document_awaiting_review(session, library_id) -> None:
    """One document the gate declined to file, for the correction specs.

    Given a title the model would plausibly produce and a person would
    plausibly want to fix, because that is the case Phase 17 exists for.
    """
    filename = "needs-a-correction.pdf"
    existing = (
        await session.execute(
            sa.select(SourceFile).where(
                SourceFile.library_id == library_id,
                SourceFile.original_filename == filename,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = SourceFile(
            library_id=library_id,
            sha256=uuid.uuid4().hex * 2,
            byte_size=2048,
            original_filename=filename,
            ingest_source=IngestSource.WEB_UPLOAD,
            page_count=1,
            state=SourceFileState.PROCESSED,
        )
        session.add(existing)
        await session.flush()
        session.add(
            Page(
                source_file_id=existing.id,
                page_number=1,
                text="Harbour Utilities quarterly statement, account 4417.",
            )
        )

    document = (
        await session.execute(
            sa.select(Document).where(Document.source_file_id == existing.id)
        )
    ).scalar_one_or_none()
    if document is None:
        document = Document(
            library_id=library_id,
            source_file_id=existing.id,
            page_start=1,
            page_end=1,
        )
        session.add(document)

    # Reset every run, not just on creation. The correction specs edit this
    # document and the screenshot capture photographs it, so a fixture that
    # kept whatever the last test typed would drift into the documentation —
    # which is how "Corrected by the e2e run" ended up in review.png once.
    document.title = "Harbour Utilties - Statment"  # the misspelling is the point
    document.review_state = ReviewState.NEEDS_REVIEW
    await session.flush()

    # And give the fields back, or the second run starts with them claimed by
    # whoever the first run signed in as.
    await session.execute(
        sa.delete(FieldSource).where(FieldSource.document_id == document.id)
    )
    print(f"{filename} is waiting for review")


asyncio.run(main())
