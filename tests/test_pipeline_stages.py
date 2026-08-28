"""The pipeline stages against real OCR (T-1.4, T-1.5).

Runs in the worker image, so Tesseract, Ghostscript and poppler are actually
present. Marked slow because each case runs a genuine OCR pass.

    make test-pipeline
"""

import json
import uuid

import pytest
import sqlalchemy as sa

from api.artifacts import derived_for
from api.config import get_settings
from api.db.enums import IngestSource, JobStage, JobState, LibraryKind, SourceFileState
from api.db.models import Job, Library, Page, SourceFile
from api.queue import ClaimedJob
from api.storage.blobs import blob_path
from tests.corpus.fixtures import CLEAN_SCAN, render_text_page
from worker.stages.normalize import run_normalize
from worker.stages.page import run_page

pytestmark = pytest.mark.slow


def _job(stage: JobStage, source_file_id: uuid.UUID) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        stage=stage,
        source_file_id=source_file_id,
        document_id=None,
        prompt_version=None,
        attempts=1,
    )


@pytest.fixture
async def ingested(session, tmp_path):
    """A synthetic scan sitting in the blob store, ready to normalize."""
    library = Library(name=f"Pipeline {uuid.uuid4().hex[:6]}", kind=LibraryKind.PERSONAL)
    session.add(library)
    await session.flush()

    # A nonce per test: the fixture is otherwise byte-identical every time, and
    # content-addressed storage would (correctly) treat the second one as a
    # duplicate of the first.
    text = f"{CLEAN_SCAN}\nFixture reference {uuid.uuid4().hex[:12]}"
    scan = render_text_page(text, tmp_path / "scan.png")
    payload = scan.read_bytes()
    import hashlib

    sha = hashlib.sha256(payload).hexdigest()
    destination = blob_path(sha)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)

    source_file = SourceFile(
        library_id=library.id,
        sha256=sha,
        byte_size=len(payload),
        mime_type="image/png",
        original_filename="scan.png",
        ingest_source=IngestSource.WATCHED_FOLDER,
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.commit()
    return source_file


async def test_normalize_produces_the_three_artifacts(session, ingested) -> None:
    await run_normalize(session, _job(JobStage.NORMALIZE, ingested.id))
    await session.commit()

    paths = derived_for(ingested.sha256)
    assert paths.normalized_pdf.is_file()
    # The searchable PDF, not the original image.
    assert paths.normalized_pdf.read_bytes().startswith(b"%PDF")
    assert paths.ocr_text.is_file() and paths.ocr_text.read_text().strip()   # REQ-014
    assert paths.word_boxes.is_file()                                        # REQ-015

    boxes = json.loads(paths.word_boxes.read_text())
    words = [w["t"] for page in boxes["pages"] for line in page["lines"] for w in line["words"]]
    assert "DEPARTMENT" in words
    # Every word carries a box, or the highlight overlay has nothing to draw.
    first = boxes["pages"][0]["lines"][0]["words"][0]
    assert first["x1"] > first["x0"] and first["y1"] > first["y0"]
    assert boxes["pages"][0]["width"] > 0

    await session.refresh(ingested)
    assert ingested.page_count == 1
    assert ingested.state is SourceFileState.PAGING


async def test_normalize_leaves_the_original_untouched(session, ingested) -> None:
    """Invariant 1. The stage that does the most work must change nothing."""
    original = blob_path(ingested.sha256)
    before = (original.read_bytes(), original.stat().st_mtime_ns)

    await run_normalize(session, _job(JobStage.NORMALIZE, ingested.id))
    await session.commit()

    assert (original.read_bytes(), original.stat().st_mtime_ns) == before


async def test_normalize_enqueues_the_page_stage(session, ingested) -> None:
    await run_normalize(session, _job(JobStage.NORMALIZE, ingested.id))
    await session.commit()

    queued = (
        await session.execute(
            sa.select(Job).where(
                Job.source_file_id == ingested.id, Job.stage == JobStage.PAGE.value
            )
        )
    ).scalar_one()
    assert queued.state is JobState.QUEUED


async def test_page_stage_creates_rows_renders_and_thumbnails(session, ingested) -> None:
    await run_normalize(session, _job(JobStage.NORMALIZE, ingested.id))
    await run_page(session, _job(JobStage.PAGE, ingested.id))
    await session.commit()

    pages = (
        await session.execute(
            sa.select(Page).where(Page.source_file_id == ingested.id).order_by(Page.page_number)
        )
    ).scalars().all()

    assert len(pages) == 1
    page = pages[0]
    assert "DEPARTMENT" in (page.text or "")
    assert page.render_path and page.thumb_path

    root = get_settings().data_root
    assert (root / page.render_path).is_file()
    assert (root / page.thumb_path).is_file()
    # Thumbnails must actually be smaller, or they are not thumbnails.
    assert (root / page.thumb_path).stat().st_size < (root / page.render_path).stat().st_size

    await session.refresh(ingested)
    assert ingested.state is SourceFileState.PROCESSED


async def test_the_page_text_is_searchable(session, ingested) -> None:
    """The end of the chain: OCR text reaches the index (REQ-019)."""
    await run_normalize(session, _job(JobStage.NORMALIZE, ingested.id))
    await run_page(session, _job(JobStage.PAGE, ingested.id))
    await session.commit()

    hit = (
        await session.execute(
            sa.select(Page.page_number).where(
                Page.source_file_id == ingested.id,
                Page.text_tsv.op("@@")(sa.func.websearch_to_tsquery("english", "discharge")),
            )
        )
    ).scalar_one_or_none()
    assert hit == 1


async def test_replaying_both_stages_reaches_an_identical_end_state(
    session, ingested
) -> None:
    """REQ-111. Replay is the whole reason the stages are separated this way."""
    await run_normalize(session, _job(JobStage.NORMALIZE, ingested.id))
    await run_page(session, _job(JobStage.PAGE, ingested.id))
    await session.commit()

    def snapshot(rows):
        return [(r.page_number, r.text, r.render_path, r.thumb_path) for r in rows]

    first = snapshot(
        (await session.execute(sa.select(Page).where(Page.source_file_id == ingested.id)
                               .order_by(Page.page_number))).scalars().all()
    )

    await run_normalize(session, _job(JobStage.NORMALIZE, ingested.id))
    await run_page(session, _job(JobStage.PAGE, ingested.id))
    await session.commit()

    second = snapshot(
        (await session.execute(sa.select(Page).where(Page.source_file_id == ingested.id)
                               .order_by(Page.page_number))).scalars().all()
    )

    assert first == second
    # And no duplicate rows: the upsert is keyed on (source_file_id, page_number).
    assert len(second) == 1
