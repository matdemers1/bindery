"""Which files `worker.reread` will touch, and why it leaves the rest (BND-T-006).

The re-read itself runs real OCR and lives in the slow tier
(`test_pipeline_stages.py`). The choice of files is cheap to check, and the one
that matters is the vault: re-reading a vaulted file would write its sealed text
back to `page.text` in the clear.
"""

import uuid

from api.db.enums import (
    IngestSource,
    JobStage,
    JobState,
    ReviewState,
    SourceFileState,
)
from api.db.models import Document, Job, SourceFile
from api.storage.blobs import blob_path
from worker.reread import candidates


async def _file(session, library, name: str, *, blob: bool = True) -> SourceFile:
    sha = uuid.uuid4().hex * 2
    if blob:
        path = blob_path(sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7 stand-in")
    source_file = SourceFile(
        library_id=library.id, sha256=sha, byte_size=17, original_filename=name,
        ingest_source=IngestSource.WATCHED_FOLDER, page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    return source_file


async def _skip(session, source_file: SourceFile) -> str | None:
    [candidate] = await candidates(session, source_file.id)
    return candidate.skip


async def test_an_ordinary_scan_is_read(session, user_factory) -> None:
    _, library = await user_factory()
    scan = await _file(session, library, "affidavit.pdf")
    assert await _skip(session, scan) is None


async def test_a_file_holding_a_vaulted_document_is_never_read(session, user_factory) -> None:
    user, library = await user_factory()
    scan = await _file(session, library, "sealed.pdf")
    session.add(Document(
        library_id=library.id, source_file_id=scan.id, page_start=1, page_end=1,
        review_state=ReviewState.FILED, vaulted_by=user.id,
    ))
    await session.flush()
    assert await _skip(session, scan) == "holds a vaulted document"


async def test_videos_busy_files_and_missing_originals_are_left_alone(
    session, user_factory
) -> None:
    _, library = await user_factory()
    video = await _file(session, library, "birthday.mp4")
    busy = await _file(session, library, "arriving.pdf")
    session.add(Job(source_file_id=busy.id, stage=JobStage.PAGE, state=JobState.QUEUED))
    missing = await _file(session, library, "gone.pdf", blob=False)
    await session.flush()

    assert await _skip(session, video) == "a video, never OCR'd"
    assert await _skip(session, busy) == "a job for it is queued or running"
    assert await _skip(session, missing) == "its original is missing"


async def test_a_finished_job_does_not_make_a_file_busy(session, user_factory) -> None:
    _, library = await user_factory()
    scan = await _file(session, library, "done.pdf")
    session.add(Job(source_file_id=scan.id, stage=JobStage.SEGMENT, state=JobState.SUCCEEDED))
    await session.flush()
    assert await _skip(session, scan) is None
