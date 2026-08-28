"""The pipeline stages against real OCR (T-1.4, T-1.5).

Runs in the worker image, so Tesseract, Ghostscript and poppler are actually
present. Marked slow because each case runs a genuine OCR pass.

    make test-pipeline
"""

import json
import shutil
import uuid

import pytest
import sqlalchemy as sa

from api.artifacts import derived_for
from api.config import get_settings
from api.db.enums import IngestSource, JobStage, JobState, LibraryKind, SourceFileState
from api.db.models import Document, Job, Library, Page, SourceFile
from api.queue import ClaimedJob
from api.storage.blobs import blob_path
from tests.corpus.fixtures import CLEAN_SCAN, render_text_page
from worker.stages.normalize import run_normalize
from worker.stages.page import run_page

# The api test image carries no OCR toolchain — these suites belong to the
# `test-worker` service (`make test-pipeline`). Skipping rather than failing
# means `-m slow` is safe to run against either image.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ocrmypdf") is None,
        reason="OCR toolchain not present; run this suite with `make test-pipeline`",
    ),
]


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
    # The page stage hands off to segmentation; `segment` is what completes a
    # file (Phase 2).
    assert ingested.state is SourceFileState.SEGMENTING


async def test_the_full_chain_ends_in_a_segmented_searchable_file(session, ingested) -> None:
    """normalize -> page -> segment, the whole pipeline as it stands today."""
    from worker.stages.segment import run_segment

    await run_normalize(session, _job(JobStage.NORMALIZE, ingested.id))
    await run_page(session, _job(JobStage.PAGE, ingested.id))
    await run_segment(session, _job(JobStage.SEGMENT, ingested.id))
    await session.commit()

    await session.refresh(ingested)
    assert ingested.state is SourceFileState.PROCESSED

    # A single-page scan is one document over pages 1..N — the common case.
    documents = (
        await session.execute(
            sa.select(Document).where(
                Document.source_file_id == ingested.id, Document.superseded_at.is_(None)
            )
        )
    ).scalars().all()
    assert [(d.page_start, d.page_end) for d in documents] == [(1, 1)]


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



# --------------------------------------------------------------------------
# The escalation, end to end (T-1.4)
# --------------------------------------------------------------------------


async def test_a_page_that_yields_nothing_is_retried_with_force_ocr(
    session, monkeypatch, tmp_path
) -> None:
    """The behaviour, not the flags: nothing extracted means try again, forced.

    Driven by stubbing extraction rather than by synthesising a PDF ocrmypdf
    refuses — the refusal depends on internals of its page analysis, so a
    fixture built to trigger it today would quietly stop triggering it later and
    the test would keep passing while testing nothing.
    """
    from worker.stages import normalize

    library = Library(name="Escalation", kind=LibraryKind.PERSONAL)
    session.add(library)
    await session.flush()

    payload = render_text_page(
        "Authorization to Contact", tmp_path / "vector-scan.png"
    ).read_bytes()
    source_file = SourceFile(
        library_id=library.id,
        sha256=uuid.uuid4().hex * 2,
        byte_size=len(payload),
        original_filename="vector-scan.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.flush()

    blob = blob_path(source_file.sha256)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(payload)

    forced_runs: list[bool] = []

    async def fake_run_ocr(source, output, sidecar, *, image, force=False):
        forced_runs.append(force)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)

    # Empty on the first extraction, real on the second — exactly the shape of
    # a page ocrmypdf declined to touch and then was made to.
    extractions = iter([
        {"pages": [{"lines": []}]},
        {"pages": [{"lines": [{"words": [{"t": "Authorization"}, {"t": "to"}]}]}]},
    ])

    async def fake_extract(_pdf):
        return next(extractions)

    monkeypatch.setattr(normalize, "_run_ocr", fake_run_ocr)
    monkeypatch.setattr(normalize, "extract_word_boxes", fake_extract)

    await normalize.run_normalize(
        session, ClaimedJob(
            id=uuid.uuid4(), stage=JobStage.NORMALIZE, source_file_id=source_file.id,
            document_id=None, prompt_version=None, attempts=0,
        )
    )

    assert forced_runs == [False, True], (
        "the first pass preserves a digital-native text layer; the second only "
        "happens because the first produced nothing"
    )
    boxes = json.loads(derived_for(source_file.sha256).word_boxes.read_text())
    assert normalize._word_count(boxes) == 2, "the recovered text is what gets stored"


async def test_a_page_that_yields_text_is_never_forced(
    session, monkeypatch, tmp_path
) -> None:
    """REQ-017 in one assertion: a digital-native PDF is not re-rasterized."""
    from worker.stages import normalize

    library = Library(name="No escalation", kind=LibraryKind.PERSONAL)
    session.add(library)
    await session.flush()

    payload = render_text_page(
        "Already searchable", tmp_path / "digital.png"
    ).read_bytes()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=len(payload),
        original_filename="digital.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.flush()
    blob = blob_path(source_file.sha256)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(payload)

    forced_runs: list[bool] = []

    async def fake_run_ocr(source, output, sidecar, *, image, force=False):
        forced_runs.append(force)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)

    async def fake_extract(_pdf):
        return {"pages": [{"lines": [{"words": [{"t": "Already"}]}]}]}

    monkeypatch.setattr(normalize, "_run_ocr", fake_run_ocr)
    monkeypatch.setattr(normalize, "extract_word_boxes", fake_extract)

    await normalize.run_normalize(
        session, ClaimedJob(
            id=uuid.uuid4(), stage=JobStage.NORMALIZE, source_file_id=source_file.id,
            document_id=None, prompt_version=None, attempts=0,
        )
    )
    assert forced_runs == [False], "one pass only; nothing was lost to recover"
