"""Normalize: OCR the original into a searchable PDF, and extract word boxes.

The slow, expensive, quality-defining stage. Everything downstream is only as
good as what comes out of here, which is why the golden-corpus accuracy figure
(REQ-018) is the gate on Phase 3.

Reads:  blobs/<aa>/<bb>/<sha256>            (never modified — invariant 1)
Writes: derived/<sha256>/normalized.pdf     searchable PDF/A, text over image
        derived/<sha256>/ocr.txt            sidecar plain text          (REQ-014)
        derived/<sha256>/ocr.json           per-word coordinates        (REQ-015)

Idempotent: re-running overwrites its own artifacts and re-derives page_count.
Swapping OCR settings therefore costs one replay, not a re-ingest — which is the
whole reason the stages are separated this way.
"""

import asyncio
import json
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from api import queue
from api.artifacts import derived_for
from api.config import get_settings
from api.db.enums import JobStage, SourceFileState
from api.db.models import SourceFile
from api.queue import ClaimedJob
from api.storage.blobs import blob_path
from worker import subprocess_util
from worker.ocr.word_boxes import extract_word_boxes

log = logging.getLogger("bindery.worker.normalize")

OCR_TIMEOUT_SECONDS = 60 * 60
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif"}
HEIF_SUFFIXES = {".heic", ".heif"}
# ocrmypdf exit code: every page already carried text, so there was nothing to do.
ALREADY_HAS_TEXT = 6
# ocrmypdf exit code: the input could not be coerced to PDF/A.
PDFA_CONVERSION_FAILED = 10

# ...but it is not the only way that failure arrives. A PDF with a DeviceN
# colour space — routine in professionally printed forms, which is to say in tax
# documents — fails PDF/A conversion with a *generic* exit 1 and one of these
# messages instead. Detecting only exit code 10 meant a real 1099 and a real W-2
# retried five times and gave up, unsearchable, for a reason that has nothing to
# do with reading them.
PDFA_FAILURE_SIGNATURES = (
    "ColorConversionNeededError",
    "ColorConversionStrategy",
    "DeviceN",
    "not permitted in PDF/A",
)


def _is_pdfa_failure(error: subprocess_util.CommandError) -> bool:
    if error.returncode == PDFA_CONVERSION_FAILED:
        return True
    message = str(error)
    return any(signature in message for signature in PDFA_FAILURE_SIGNATURES)



def _word_count(boxes: dict) -> int:
    """How much text the normalized PDF actually yielded.

    Zero is the signal that OCR achieved nothing — whether it ran and found an
    empty page, or declined to run at all.
    """
    return sum(
        len(line["words"])
        for page in boxes.get("pages", [])
        for line in page.get("lines", [])
    )


def _looks_like_image(source_file: SourceFile) -> bool:
    if source_file.mime_type and source_file.mime_type.startswith("image/"):
        return True
    name = source_file.original_filename or ""
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


def _ocr_argv(
    source: Path, output: Path, sidecar: Path, *, pdfa: bool, image: bool, force: bool = False
) -> list[str]:
    settings = get_settings()
    argv = [
        "ocrmypdf",
        "--language", settings.ocr_languages,
        # Pages that already carry a text layer are passed through untouched:
        # re-rasterizing a digital-native PDF destroys quality (REQ-017).
        #
        # `--force-ocr` is the escalation for the case that rule cannot see:
        # a page whose content is vector paths rather than an image. ocrmypdf
        # refuses those by default — "no images - skipping all processing on
        # this page to avoid losing detail" — and exits 0, so a scan comes out
        # the far end with no text and nothing reported. See `_run_ocr`.
        "--force-ocr" if force else "--skip-text",
        "--sidecar", str(sidecar),
        "--output-type", "pdfa" if pdfa else "pdf",
        "--jobs", "1",  # parallelism is the queue's job, not ocrmypdf's
        "--quiet",
    ]
    if settings.ocr_deskew:
        argv.append("--deskew")
    if settings.ocr_clean:
        # `--clean` cleans the image fed to Tesseract but leaves the page image
        # in the output alone, so the original resolution survives (REQ-012).
        argv.append("--clean")
    if image:
        # A scanner JPEG usually has no DPI metadata; without this ocrmypdf
        # cannot size the page.
        argv += ["--image-dpi", "300"]
    argv += [str(source), str(output)]
    return argv


async def _run_ocr(
    source: Path, output: Path, sidecar: Path, *, image: bool, force: bool = False
) -> None:
    try:
        code, _, _ = await subprocess_util.run(
            _ocr_argv(source, output, sidecar, pdfa=True, image=image, force=force),
            timeout=OCR_TIMEOUT_SECONDS,
            ok_codes=(0, ALREADY_HAS_TEXT),
        )
    except subprocess_util.CommandError as exc:
        if not _is_pdfa_failure(exc):
            raise
        # PDF/A is a nice-to-have for archival fidelity; searchability is not
        # negotiable. Fall back rather than fail the document — and note that
        # for a DeviceN document plain PDF is arguably the *better* artifact,
        # since it keeps the original colour space instead of mangling it.
        log.warning(
            "PDF/A conversion failed for %s; falling back to plain PDF (%s)",
            source.name, str(exc)[:200],
        )
        code, _, _ = await subprocess_util.run(
            _ocr_argv(source, output, sidecar, pdfa=False, image=image, force=force),
            timeout=OCR_TIMEOUT_SECONDS,
            ok_codes=(0, ALREADY_HAS_TEXT),
        )

    if code == ALREADY_HAS_TEXT and not output.exists():
        # Nothing to add: the original is already the searchable artifact.
        output.write_bytes(source.read_bytes())
        log.info("%s already carried a full text layer; copied through", source.name)


def _is_heif(source_file: SourceFile, original: Path) -> bool:
    if (source_file.mime_type or "") in {"image/heic", "image/heif"}:
        return True
    return Path(source_file.original_filename or "").suffix.lower() in HEIF_SUFFIXES


@asynccontextmanager
async def _ocr_input(source_file: SourceFile, original: Path):
    """Yield a path ocrmypdf can actually read.

    HEIC needs decoding first: ocrmypdf shells out to its own interpreter, which
    never calls `register_heif_opener`, so the format is invisible to it even
    with pillow-heif installed here. Converting to JPEG in this process is the
    whole fix (REQ-006).
    """
    if not _is_heif(source_file, original):
        yield original
        return

    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    with tempfile.TemporaryDirectory(dir=get_settings().temp_root) as scratch:
        converted = Path(scratch) / "converted.jpg"

        def _convert() -> None:
            with Image.open(original) as image:
                image.convert("RGB").save(converted, "JPEG", quality=95)

        await asyncio.to_thread(_convert)
        yield converted


async def run_normalize(session: AsyncSession, job: ClaimedJob) -> None:
    source_file = await session.get(SourceFile, job.source_file_id)
    if source_file is None:
        raise ValueError(f"source file {job.source_file_id} no longer exists")

    original = blob_path(source_file.sha256)
    if not original.is_file():
        # The row says we have these bytes and we do not. That is an integrity
        # problem, not a processing problem.
        raise FileNotFoundError(f"blob missing for {source_file.sha256}")

    paths = derived_for(source_file.sha256)
    paths.mkdirs()

    source_file.state = SourceFileState.NORMALIZING
    await session.flush()

    get_settings().temp_root.mkdir(parents=True, exist_ok=True)
    async with _ocr_input(source_file, original) as ocr_source:
        await _run_ocr(
            ocr_source,
            paths.normalized_pdf,
            paths.ocr_text,
            image=_looks_like_image(source_file),
        )

    # Word boxes come from the normalized PDF's text layer, so the same
    # extraction works for OCR'd scans and digital-native PDFs alike — and it
    # happens now, in the same pass, because regenerating it later would mean
    # re-running OCR (exactly what replayable stages exist to avoid).
    boxes = await extract_word_boxes(paths.normalized_pdf)

    if _word_count(boxes) == 0:
        # Nothing at all came out. The likeliest cause is a page ocrmypdf
        # declined to touch: content drawn as vector paths rather than as an
        # image makes it skip the page "to avoid losing detail" — and it exits
        # 0, so the file arrives fully processed and completely unsearchable.
        # That is the worst possible outcome for an archive, and it happened to
        # a real scanned form.
        #
        # Escalating is safe precisely because there was nothing to lose: a
        # digital-native PDF has a text layer, which means a non-zero word
        # count, which means this branch is not taken. Only a page that gave up
        # nothing gets rasterized — and the *original* is untouched either way,
        # since this rewrites the derived artifact.
        log.warning(
            "%s produced no text; retrying with --force-ocr",
            source_file.original_filename,
        )
        async with _ocr_input(source_file, original) as ocr_source:
            await _run_ocr(
                ocr_source,
                paths.normalized_pdf,
                paths.ocr_text,
                image=_looks_like_image(source_file),
                force=True,
            )
        boxes = await extract_word_boxes(paths.normalized_pdf)
        log.info(
            "%s recovered %s words with --force-ocr",
            source_file.original_filename, _word_count(boxes),
        )

    paths.word_boxes.write_text(json.dumps(boxes, separators=(",", ":")))

    page_count = len(boxes["pages"])
    if page_count == 0:
        raise ValueError("normalized PDF reported zero pages")

    source_file.page_count = page_count
    source_file.state = SourceFileState.PAGING
    await session.flush()

    # `requeue_stage`, not `enqueue`: enqueue is idempotent and deliberately
    # refuses to disturb an existing job, which is right for the first run and
    # wrong for a replay. On a rescan the downstream job already exists and
    # already succeeded, so `enqueue` is a no-op and the replay stops dead here
    # — the file gets re-OCR'd and nothing downstream ever sees the new text.
    # On a first run there is no existing job, so the two behave identically.
    await queue.requeue_stage(session, JobStage.PAGE, source_file_id=source_file.id)
    log.info("normalized %s (%s pages)", source_file.original_filename, page_count)
