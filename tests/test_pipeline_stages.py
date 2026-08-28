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

    async def fake_run_ocr(source, output, sidecar, *, image, force=False, scanned=True):
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

    async def fake_run_ocr(source, output, sidecar, *, image, force=False, scanned=True):
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


# --------------------------------------------------------------------------
# Office documents (T-8.14)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,builder",
    [
        ("statement.csv", "csv"),
        ("letter.docx", "docx"),
        ("accounts.xlsx", "xlsx"),
        ("notes.txt", "txt"),
        # Added after auditing a real archive: a saved pay statement was sitting
        # in a banking folder as .mht, and a proposal as .pages, both unreachable.
        ("statement.html", "html"),
        ("screenshot.webp", "webp"),
        ("scan.gif", "gif"),
    ],
)
async def test_office_documents_become_searchable(session, tmp_path, name, builder) -> None:
    """The point of converting rather than parsing.

    A spreadsheet of account numbers is exactly what an archive is for, and it
    used to be skipped as unsupported. Rendering it to PDF means it arrives
    searchable, citable and viewable by the same code as a scan — instead of
    needing its own paging, viewer and citation model.
    """
    from worker.stages import normalize

    payload = _office_fixture(builder, tmp_path / name)

    library = Library(name=f"Office {name}", kind=LibraryKind.PERSONAL)
    session.add(library)
    await session.flush()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=len(payload),
        original_filename=name, ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.flush()
    blob = blob_path(source_file.sha256)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(payload)

    await normalize.run_normalize(
        session,
        ClaimedJob(
            id=uuid.uuid4(), stage=JobStage.NORMALIZE, source_file_id=source_file.id,
            document_id=None, prompt_version=None, attempts=0,
        ),
    )
    await session.commit()

    boxes = json.loads(derived_for(source_file.sha256).word_boxes.read_text())
    assert normalize._word_count(boxes) > 0, f"{name} produced no searchable text"

    text = " ".join(
        word["t"]
        for page in boxes["pages"]
        for line in page["lines"]
        for word in line["words"]
    )
    assert "Meridian" in text, f"the actual content of {name} did not survive"

    # The original is untouched: conversion writes a derived artifact only.
    assert blob.read_bytes() == payload


def _office_fixture(kind: str, path) -> bytes:
    """Build a real file of each type, not a stub — the conversion is the test."""
    if kind == "csv":
        path.write_text("Account,Institution,Balance\n4417,Meridian Credit Union,1284.55\n")
    elif kind == "txt":
        path.write_text("Meridian Credit Union — account notes\nOpened 2019.\n")
    elif kind == "docx":
        from docx import Document as Docx

        document = Docx()
        document.add_paragraph("Meridian Credit Union")
        document.add_paragraph("Account closing confirmation.")
        document.save(path)
    elif kind == "xlsx":
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet["A1"] = "Institution"
        sheet["A2"] = "Meridian Credit Union"
        book.save(path)
    elif kind == "html":
        path.write_text(
            "<html><body><h1>Meridian Credit Union</h1>"
            "<p>Statement of account, 2019.</p></body></html>"
        )
    elif kind in {"webp", "gif"}:
        # Reuse the corpus renderer, which is already tuned to produce pages
        # Tesseract can actually read — a hand-rolled PIL image at default font
        # size reaches OCR fine and comes back as gibberish, which would test
        # the format plumbing while looking like a format failure.
        from PIL import Image

        rendered = render_text_page("Meridian Credit Union", path.with_suffix(".png"))
        with Image.open(rendered) as image:
            image.convert("RGB").save(path, kind.upper())
    return path.read_bytes()


async def test_a_converted_document_is_not_put_through_ocr(session, tmp_path) -> None:
    """The incident this prevents.

    LibreOffice had just laid the text out; recognising it again can only be
    slower and less accurate. Worse, `--deskew` and `--clean` force ocrmypdf to
    rasterize every page even under `--skip-text`, so a 1,127-page deck
    converted from PowerPoint pinned a CPU for forty minutes producing a
    photograph of text that was already perfect — with 499 files queued behind
    it and only three worker slots.
    """
    from worker.stages import normalize

    payload = _office_fixture("docx", tmp_path / "memo.docx")
    library = Library(name="No OCR", kind=LibraryKind.PERSONAL)
    session.add(library)
    await session.flush()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=len(payload),
        original_filename="memo.docx", ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.flush()
    blob = blob_path(source_file.sha256)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(payload)

    ran_ocr = False

    async def fail_if_called(*args, **kwargs):
        nonlocal ran_ocr
        ran_ocr = True

    original_run_ocr = normalize._run_ocr
    normalize._run_ocr = fail_if_called
    try:
        await normalize.run_normalize(
            session,
            ClaimedJob(
                id=uuid.uuid4(), stage=JobStage.NORMALIZE, source_file_id=source_file.id,
                document_id=None, prompt_version=None, attempts=0,
            ),
        )
    finally:
        normalize._run_ocr = original_run_ocr

    assert not ran_ocr, "a converted document must not be sent through OCR"

    # And it is still fully searchable, with every artifact a scan would have.
    boxes = json.loads(derived_for(source_file.sha256).word_boxes.read_text())
    assert normalize._word_count(boxes) > 0
    assert derived_for(source_file.sha256).ocr_text.read_text().strip()


def test_deskew_and_clean_are_scanner_corrections_only() -> None:
    """They rasterize every page, which is the whole cost of the incident.

    On a scan they earn it. On a generated PDF there is no skew to correct and
    no speckle to clean — only crisp vector glyphs to replace with a picture.
    """
    from pathlib import Path as P

    from worker.stages.normalize import _ocr_argv

    scanned = _ocr_argv(P("i"), P("o"), P("s"), pdfa=True, image=False, scanned=True)
    generated = _ocr_argv(P("i"), P("o"), P("s"), pdfa=True, image=False, scanned=False)

    assert "--deskew" in scanned and "--clean" in scanned
    assert "--deskew" not in generated and "--clean" not in generated
    # Nothing else changes: this is one decision, not a different operation.
    assert [a for a in scanned if a not in {"--deskew", "--clean"}] == generated
