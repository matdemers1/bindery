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
import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from api import queue
from api.artifacts import derived_for
from api.config import get_settings
from api.db.enums import JobStage, SourceFileState
from api.db.models import SourceFile
from api.queue import ClaimedJob
from api.storage.blobs import blob_path
from worker import convert, subprocess_util
from worker.ocr.word_boxes import extract_word_boxes

log = logging.getLogger("bindery.worker.normalize")

OCR_TIMEOUT_SECONDS = 60 * 60
IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif",
    # A screenshot or a phone photo saved in one of these carries no DPI
    # metadata either, so it needs the same --image-dpi treatment.
    ".gif", ".webp", ".bmp",
}
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
    # Ghostscript giving up on the PDF/A rendering entirely, which it reports
    # as exit 7 rather than 10 and with none of the wording above.
    "PDF/A rendering failed",
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
    source: Path,
    output: Path,
    sidecar: Path,
    *,
    pdfa: bool,
    image: bool,
    force: bool = False,
    scanned: bool = True,
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
    # Deskew and clean correct for a *scanner*: a page fed in crooked, speckle
    # from a platen. They are meaningless on a digitally generated PDF — and far
    # from free, because both force ocrmypdf to rasterize every page even when
    # `--skip-text` would otherwise pass it straight through. A 1,127-page deck
    # converted from PowerPoint pinned a core for forty minutes doing exactly
    # that, to a file that already had a perfect text layer.
    if scanned:
        if settings.ocr_deskew:
            argv.append("--deskew")
        if settings.ocr_clean:
            # `--clean` cleans the image fed to Tesseract but leaves the page
            # image in the output alone, so the original resolution survives
            # (REQ-012).
            argv.append("--clean")
    if image:
        # A scanner JPEG usually has no DPI metadata; without this ocrmypdf
        # cannot size the page.
        argv += ["--image-dpi", "300"]
    argv += [str(source), str(output)]
    return argv


async def _run_ocr(
    source: Path,
    output: Path,
    sidecar: Path,
    *,
    image: bool,
    force: bool = False,
    scanned: bool = True,
) -> None:
    """Run OCR, applying a known remedy for each refusal that has one.

    Remedies accumulate rather than replace each other. A signed PDF that then
    fails PDF/A conversion needs both fixes, and trying them one at a time in
    isolation would report the second failure as if the first had not been
    solved — which is exactly what happened to two signed military forms.
    """
    overrides: dict = {"pdfa": True, "image": image, "force": force, "scanned": scanned}
    extra: list[str] = []
    applied: set[str] = set()

    # Bounded: one attempt per distinct remedy, plus the original.
    for _ in range(len(("signature", "xfa", "pdfa")) + 1):
        try:
            code, _out, _err = await subprocess_util.run(
                _ocr_argv(source, output, sidecar, **overrides) + extra,
                timeout=OCR_TIMEOUT_SECONDS,
                ok_codes=(0, ALREADY_HAS_TEXT),
            )
            break
        except subprocess_util.CommandError as exc:
            remedy = _remedy_for(exc)
            if remedy is None:
                raise
            key, argv_overrides, flags, why = remedy
            if key in applied:
                # Already tried this; it is not going to work a second time.
                raise
            applied.add(key)
            overrides.update(argv_overrides)
            extra += flags
            log.warning("%s: %s (%s)", source.name, why, str(exc)[:160])
    else:
        raise RuntimeError(f"OCR for {source.name} exhausted every known remedy")

    if code == ALREADY_HAS_TEXT and not output.exists():
        # Nothing to add: the original is already the searchable artifact.
        output.write_bytes(source.read_bytes())
        log.info("%s already carried a full text layer; copied through", source.name)


def _remedy_for(error: subprocess_util.CommandError) -> tuple[str, dict, list[str], str] | None:
    """One more attempt for the refusals that have a safe answer, or None.

    Returns `(key, argv_overrides, extra_flags, explanation)`. The key exists so
    a remedy is applied at most once — a fix that did not work will not work on
    the second try either, and a loop that keeps re-applying it would spin.

    Every one of these is a case where ocrmypdf is right to refuse by default
    and wrong for this application, because of a property the rest of the system
    guarantees: **the original is never modified.** Everything written here is a
    derived artifact sitting beside a blob that still holds the exact bytes that
    arrived.
    """
    message = str(error)

    if "XFA" in message or "LiveCycle" in message:
        # Nothing to remedy. A dynamic XFA form carries no static content —
        # every renderer outside Adobe sees a "please open this in Acrobat"
        # placeholder — so neither skipping nor forcing OCR can reach the
        # contents. Forcing was tried and produced the same refusal.
        raise PermanentFailure(
            "this is a dynamic XFA form built with Adobe LiveCycle; its contents "
            "are only readable in Adobe Acrobat or Reader. Re-saving or printing "
            "it to a normal PDF from Acrobat would make it importable."
        )

    if "DigitalSignatureError" in message:
        # OCR would invalidate the signature — on the *copy*. The signed
        # original stays in the blob store, byte for byte, and is what an
        # export hands back. Refusing instead would leave every signed form you
        # own unsearchable, to protect a signature on a file nobody will ever
        # verify from here.
        return (
            "signature",
            {},
            ["--invalidate-digital-signatures"],
            "signed PDF: OCR-ing a copy, the signed original is untouched",
        )

    if _is_pdfa_failure(error):
        # PDF/A is a nice-to-have for archival fidelity; searchability is not
        # negotiable. For a DeviceN document plain PDF is arguably the better
        # artifact anyway, since it keeps the original colour space.
        return (
            "pdfa",
            {"pdfa": False},
            [],
            "PDF/A conversion failed, falling back to plain PDF",
        )

    return None


def _is_heif(source_file: SourceFile, original: Path) -> bool:
    if (source_file.mime_type or "") in {"image/heic", "image/heif"}:
        return True
    return Path(source_file.original_filename or "").suffix.lower() in HEIF_SUFFIXES



def _prepare_image(source_file: SourceFile, original: Path):
    """Return an image ocrmypdf will accept, or None if it already would.

    Two things it will not accept, both of which cost real documents:

    **An alpha channel.** ocrmypdf refuses outright — *"The input image has an
    alpha channel. Remove the alpha channel first."* — and 126 files in one
    import failed on it, almost all screenshots saved as PNG. Transparency is
    meaningless once a page is printed onto white paper, so it is composited
    onto white rather than simply dropped: discarding the channel turns
    transparent pixels black and can bury the text entirely.

    **HEIC.** ocrmypdf shells out to its own interpreter, which never calls
    `register_heif_opener`, so the format is invisible to it even with
    pillow-heif installed here (REQ-006).

    Anything already acceptable returns None and is passed through untouched —
    re-encoding a clean JPEG would only lose detail before OCR reads it.
    """
    from PIL import Image

    if _is_heif(source_file, original):
        import pillow_heif

        pillow_heif.register_heif_opener()

    try:
        image = Image.open(original)
        image.load()
    except Exception as error:
        # Not decodable here does not mean unusable: let ocrmypdf try and
        # report its own, more specific, failure.
        log.debug("could not pre-read %s: %s", original.name, error)
        return None

    # A page has to be big enough to be a page. img2pdf hands these to pikepdf,
    # which rejects the resulting page size with a bare ValueError and a
    # traceback that says nothing about the cause — so they are refused here,
    # in terms a person can act on. A 10x5 pixel file is a laser-cutter
    # artifact, not a document, and no amount of OCR will find text in it.
    if min(image.size) < MIN_IMAGE_PIXELS:
        width, height = image.size
        image.close()
        raise PermanentFailure(
            f"{width}x{height} pixels is too small to be a document page "
            f"(each side must be at least {MIN_IMAGE_PIXELS}px). This is "
            f"usually a design or laser-cutting asset rather than a document."
        )

    has_alpha = image.mode in {"RGBA", "LA", "PA"} or (
        image.mode == "P" and "transparency" in image.info
    )
    # ocrmypdf refuses CMYK without an embedded ICC profile rather than guess a
    # conversion. Pillow's guess is the same one every viewer already makes, and
    # a scan that reads is worth more than a colour space that is exactly right.
    is_cmyk = image.mode == "CMYK"

    if not has_alpha and not is_cmyk and not _is_heif(source_file, original):
        image.close()
        return None

    if is_cmyk and not has_alpha:
        converted = image.convert("RGB")
        image.close()
        log.info("converted %s from CMYK to RGB", source_file.original_filename)
        return converted

    if has_alpha:
        rgba = image.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.split()[-1])
        rgba.close()
        image.close()
        log.info("flattened transparency in %s", source_file.original_filename)
        return flattened

    converted = image.convert("RGB")
    image.close()
    return converted


@dataclass(frozen=True)
class OcrInput:
    """What to feed the OCR stage, and whether it needs OCR at all."""

    path: Path
    # True when the text in this PDF was generated rather than photographed —
    # a converted office document. Its text layer is already exact, so running
    # recognition over it can only be slower and worse.
    digital_native: bool


@asynccontextmanager
async def _ocr_input(source_file: SourceFile, original: Path):
    """Yield a path ocrmypdf can actually read.

    Two formats need turning into something else first, and this is the one
    place that knows about it:

    **Office documents** — a Word letter, a spreadsheet of account numbers, a
    CSV exported from a bank — are rendered to PDF so they travel the ordinary
    path and end up searchable, citable and viewable like everything else.

    **HEIC** needs decoding because ocrmypdf shells out to its own interpreter,
    which never calls `register_heif_opener`, so the format is invisible to it
    even with pillow-heif installed here (REQ-006).

    The original is untouched in both cases: what is yielded is a temporary
    file, and the blob keeps the bytes you put in.
    """
    if convert.is_convertible(source_file.original_filename, source_file.mime_type):
        with tempfile.TemporaryDirectory(dir=get_settings().temp_root) as scratch:
            converted = await convert.to_pdf(
                original,
                Path(scratch),
                original_name=source_file.original_filename,
            )
            # Digital-native by construction: LibreOffice just laid this text
            # out, so it is already perfect and there is nothing to recognize.
            yield OcrInput(converted, digital_native=True)
        return

    if not _looks_like_image(source_file):
        yield OcrInput(original, digital_native=False)
        return

    prepared = await asyncio.to_thread(_prepare_image, source_file, original)
    if prepared is None:
        yield OcrInput(original, digital_native=False)
        return

    with tempfile.TemporaryDirectory(dir=get_settings().temp_root) as scratch:
        converted = Path(scratch) / "converted.jpg"
        await asyncio.to_thread(prepared.save, converted, "JPEG", quality=95)
        prepared.close()
        yield OcrInput(converted, digital_native=False)




# Below this, a PDF page cannot be constructed at all — and nothing this small
# holds readable text anyway.
MIN_IMAGE_PIXELS = 16


class PermanentFailure(Exception):
    """This input cannot be processed, and trying again will not change that.

    Distinct from an ordinary failure so the queue can stop immediately rather
    than spend five attempts and half an hour reaching the same conclusion.
    """


# Enough text that the pages plainly came from a computer rather than a camera.
# A scan with a stray character or two of junk OCR should not qualify.
DIGITAL_TEXT_THRESHOLD = 200


async def _already_has_text(pdf: Path) -> bool:
    """Whether this PDF arrived with a usable text layer.

    Cheap — one `pdftotext` — and it decides whether the scanner corrections
    are worth their cost. Failing this check is not an error: an unreadable or
    non-PDF input simply gets treated as a scan, which is what it probably is.
    """
    try:
        _, stdout, _ = await subprocess_util.run(
            ["pdftotext", str(pdf), "-"], timeout=120
        )
    except Exception:
        return False
    return len(b"".join(stdout.split())) >= DIGITAL_TEXT_THRESHOLD


async def _write_sidecar(pdf: Path, sidecar: Path) -> None:
    """The plain-text artifact ocrmypdf would have written (REQ-014).

    Produced here directly when OCR is skipped, so a converted document still
    has every artifact a scanned one does and nothing downstream has to know
    which path it took.
    """
    _, stdout, _ = await subprocess_util.run(
        ["pdftotext", "-layout", str(pdf), "-"], timeout=300
    )
    sidecar.write_bytes(stdout)


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
        if ocr_source.digital_native:
            # No OCR. LibreOffice just laid this text out, so recognition could
            # only be slower and less accurate than the text that is already
            # there — and rasterizing it would replace crisp vector glyphs with
            # a photograph of themselves.
            await asyncio.to_thread(
                shutil.copyfile, ocr_source.path, paths.normalized_pdf
            )
            await _write_sidecar(paths.normalized_pdf, paths.ocr_text)
            log.info(
                "%s converted to PDF with its own text layer; skipping OCR",
                source_file.original_filename,
            )
        else:
            # A PDF that already carries text is digital-native too, even
            # though nothing here converted it — someone exported it from
            # their bank, or printed it to file. `--skip-text` will pass those
            # pages through untouched, so deskewing and cleaning them buys
            # nothing and costs a full rasterization of every page.
            await _run_ocr(
                ocr_source.path,
                paths.normalized_pdf,
                paths.ocr_text,
                image=_looks_like_image(source_file),
                scanned=not await _already_has_text(ocr_source.path),
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
                ocr_source.path,
                paths.normalized_pdf,
                paths.ocr_text,
                image=_looks_like_image(source_file),
                force=True,
                # A converted document that yielded nothing is a deck of
                # pictures, not a crooked scan.
                scanned=not ocr_source.digital_native,
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
