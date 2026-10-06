"""Re-read the text of files already in the archive, and touch nothing else.

    make reread-text dry=1                 # what would be read, and what is skipped
    make reread-text file=<source-file id> # one file first, to look at the result
    make reread-text                       # every file

For an OCR fix that has to reach what is already stored (BND-T-006: every
scanned page held "o ce" for "office" because the text layer lost its
ligatures). It replays `normalize` and `page` through their real code paths,
re-embeds the live documents, and **stops there**.

Not a rescan of every file, which is what `/rescan` or a requeued `normalize`
would be. Those cascade into `segment`, and a machine re-segmentation retires
every document and creates new ones at "waiting for classification" — the
filing, tags and corrections of the whole archive gone in one batch, which is
the kill criterion arriving by maintenance script. And `make enqueue-stage
stage=normalize` does nothing at all: `enqueue` refuses to disturb a job that
has already run.

Skipped, each with its reason printed:

- **A file holding a vaulted document.** Its plaintext original is gone and its
  text is sealed; re-reading it would write that text back to `page.text`, in
  the clear, which is the thing the vault exists to prevent.
- **A video.** It was never OCR'd; its page is its metadata summary.
- **A file with a queued or running job.** The worker is already at it.
- **A file whose original is missing.** An integrity problem, reported by the
  integrity check, not something to paper over here.

Each file is its own transaction and its own audit event (`text_reread`,
naming the pages whose text changed). One failure is reported and the run goes
on; any failure makes the exit status non-zero.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.db.enums import ActorType, JobStage, JobState
from api.db.models import Document, Job, Page, SourceFile
from api.db.session import SessionFactory
from api.queue import ClaimedJob
from api.storage.blobs import blob_path
from worker import media
from worker.stages.embed import run_embed
from worker.stages.normalize import run_normalize
from worker.stages.page import run_page


@dataclass(frozen=True)
class Candidate:
    source_file_id: uuid.UUID
    name: str
    skip: str | None  # why it is left alone, or None to read it


async def candidates(
    session: AsyncSession, only: uuid.UUID | None = None
) -> list[Candidate]:
    """Every file, oldest first, with the reason it is skipped if it is."""
    query = sa.select(SourceFile).order_by(SourceFile.received_at)
    if only is not None:
        query = query.where(SourceFile.id == only)
    files = (await session.execute(query)).scalars().all()

    vaulted = set(
        (
            await session.execute(
                sa.select(Document.source_file_id).where(Document.vaulted_by.is_not(None))
            )
        ).scalars()
    )
    busy = set(
        (
            await session.execute(
                sa.select(Job.source_file_id).where(
                    Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
                    Job.source_file_id.is_not(None),
                )
            )
        ).scalars()
    )

    found = []
    for source_file in files:
        name = source_file.original_filename or str(source_file.id)
        if source_file.id in vaulted:
            skip = "holds a vaulted document"
        elif media.is_video(source_file.original_filename, source_file.mime_type):
            skip = "a video, never OCR'd"
        elif source_file.id in busy:
            skip = "a job for it is queued or running"
        elif not blob_path(source_file.sha256).is_file():
            skip = "its original is missing"
        else:
            skip = None
        found.append(Candidate(source_file.id, name, skip))
    return found


def _job(stage: JobStage, source_file_id: uuid.UUID) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        stage=stage,
        source_file_id=source_file_id,
        document_id=None,
        prompt_version=None,
        attempts=1,
    )


async def _page_texts(session: AsyncSession, source_file_id: uuid.UUID) -> dict[int, str]:
    rows = await session.execute(
        sa.select(Page.page_number, Page.text).where(Page.source_file_id == source_file_id)
    )
    return {number: text or "" for number, text in rows}


async def reread(session: AsyncSession, source_file_id: uuid.UUID) -> list[int]:
    """Re-read one file's text in the caller's transaction; the pages that changed.

    The file's state is put back as it was: `normalize` and `page` advance it
    towards segmentation, and nothing here is going to segment it.
    """
    source_file = await session.get(SourceFile, source_file_id)
    if source_file is None:
        raise ValueError(f"source file {source_file_id} no longer exists")
    state = source_file.state
    before = await _page_texts(session, source_file_id)

    await run_normalize(session, _job(JobStage.NORMALIZE, source_file_id), cascade=False)
    await run_page(session, _job(JobStage.PAGE, source_file_id), cascade=False)
    await run_embed(session, _job(JobStage.EMBED, source_file_id), cascade=False)

    await session.execute(
        sa.update(SourceFile).where(SourceFile.id == source_file_id).values(state=state.value)
    )
    after = await _page_texts(session, source_file_id)
    changed = sorted(n for n in after if after[n] != before.get(n))

    await record(
        session,
        entity_type="source_file",
        entity_id=source_file_id,
        action="text_reread",
        actor_type=ActorType.SYSTEM,
        before={"pages": len(before)},
        after={"pages": len(after), "changed_pages": changed},
    )
    await session.flush()
    return changed


async def main(dry_run: bool, only: uuid.UUID | None) -> int:
    async with SessionFactory() as session:
        found = await candidates(session, only)

    failed = changed_files = 0
    for candidate in found:
        if candidate.skip:
            print(f"skip  {candidate.name}: {candidate.skip}")
            continue
        if dry_run:
            print(f"read  {candidate.name}")
            continue
        async with SessionFactory() as session:
            try:
                changed = await reread(session, candidate.source_file_id)
                await session.commit()
            except Exception as error:  # reported, counted, and the run goes on
                await session.rollback()
                failed += 1
                print(f"FAIL  {candidate.name}: {error}", file=sys.stderr)
                continue
        changed_files += bool(changed)
        print(f"read  {candidate.name}: {len(changed)} page(s) changed")

    readable = sum(1 for c in found if not c.skip)
    skipped = len(found) - readable
    if dry_run:
        print(f"\nwould read {readable} file(s); {skipped} skipped")
    else:
        print(
            f"\nread {readable - failed} file(s), {changed_files} with changed text; "
            f"{skipped} skipped; {failed} failed"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-file", type=uuid.UUID)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run, args.source_file)))
