"""Page: turn a normalized PDF into indexed rows and images.

> The unit of search is the **page**, not the document.

This is where that becomes true. A 100-page bundle produces 100 rows, each with
its own text, its own `tsvector`, its own render and thumbnail, each
independently addressable — so a search can answer "page 47 of Army Records
2019.pdf" and the viewer can open *there* (REQ-019, REQ-020).

Reads:  derived/<sha256>/normalized.pdf, ocr.json
Writes: `page` rows, derived/<sha256>/pages/NNNN.webp, thumbs/NNNN.webp

Idempotent: page rows are upserted on `(source_file_id, page_number)` and
renders that already exist are left alone, so a replay after an interruption
resumes rather than redoes.
"""

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import sqlalchemy as sa
from PIL import Image
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api import queue
from api.artifacts import derived_for, relative_to_data
from api.db.enums import JobStage, SourceFileState
from api.db.models import Page, SourceFile
from api.queue import ClaimedJob
from worker import subprocess_util
from worker.ocr.word_boxes import page_text

log = logging.getLogger("bindery.worker.page")

RENDER_DPI = 150
THUMB_WIDTH = 240
RENDER_QUALITY = 82
RENDER_TIMEOUT_SECONDS = 120


async def _render_page(pdf: Path, number: int, render: Path, thumb: Path) -> None:
    """Rasterize one page to webp, plus a thumbnail.

    One page at a time rather than one pdftoppm call for the whole file: peak
    disk stays bounded on a 300-page bundle, and an interrupted run resumes at
    the page it stopped on.
    """
    with tempfile.TemporaryDirectory(dir=render.parent) as scratch:
        prefix = Path(scratch) / "page"
        await subprocess_util.run(
            [
                "pdftoppm", "-png", "-r", str(RENDER_DPI),
                "-f", str(number), "-l", str(number),
                str(pdf), str(prefix),
            ],
            timeout=RENDER_TIMEOUT_SECONDS,
        )
        produced = sorted(Path(scratch).glob("page*.png"))
        if not produced:
            raise RuntimeError(f"pdftoppm produced no image for page {number}")

        def _convert() -> None:
            with Image.open(produced[0]) as image:
                image.save(render, "WEBP", quality=RENDER_QUALITY, method=4)
                thumbnail = image.copy()
                thumbnail.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 4))
                thumbnail.save(thumb, "WEBP", quality=70, method=4)

        # Pillow is synchronous and CPU-bound; keep it off the event loop so the
        # other OCR slots keep making progress.
        await asyncio.to_thread(_convert)


async def run_page(session: AsyncSession, job: ClaimedJob) -> None:
    source_file = await session.get(SourceFile, job.source_file_id)
    if source_file is None:
        raise ValueError(f"source file {job.source_file_id} no longer exists")

    paths = derived_for(source_file.sha256)
    if not paths.word_boxes.is_file() or not paths.normalized_pdf.is_file():
        raise FileNotFoundError("normalize artifacts are missing; re-run normalize first")
    paths.mkdirs()

    boxes = json.loads(paths.word_boxes.read_text())
    pages = boxes["pages"]

    for page in pages:
        number = page["number"]
        render = paths.page_render(number)
        thumb = paths.page_thumb(number)
        if not (render.is_file() and thumb.is_file()):
            await _render_page(paths.normalized_pdf, number, render, thumb)

        # text_tsv is a generated column, so the index follows `text` with no
        # chance of the two drifting apart.
        await session.execute(
            insert(Page)
            .values(
                source_file_id=source_file.id,
                page_number=number,
                text=page_text(page),
                word_boxes_path=relative_to_data(paths.word_boxes),
                render_path=relative_to_data(render),
                thumb_path=relative_to_data(thumb),
            )
            .on_conflict_do_update(
                index_elements=[Page.source_file_id, Page.page_number],
                set_={
                    "text": page_text(page),
                    "word_boxes_path": relative_to_data(paths.word_boxes),
                    "render_path": relative_to_data(render),
                    "thumb_path": relative_to_data(thumb),
                },
            )
        )

    await session.execute(
        sa.update(SourceFile)
        .where(SourceFile.id == source_file.id)
        .values(page_count=len(pages), state=SourceFileState.SEGMENTING.value)
    )
    # `requeue_stage`, not `enqueue`: enqueue is idempotent and deliberately
    # refuses to disturb an existing job, which is right for the first run and
    # wrong for a replay. On a rescan the downstream job already exists and
    # already succeeded, so `enqueue` is a no-op and the replay stops dead here
    # — the file gets re-OCR'd and nothing downstream ever sees the new text.
    # On a first run there is no existing job, so the two behave identically.
    await queue.requeue_stage(session, JobStage.SEGMENT, source_file_id=source_file.id)
    log.info("paged %s into %s rows", source_file.original_filename, len(pages))
