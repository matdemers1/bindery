"""Turning office documents into something the pipeline can read (T-8.14).

A household archive is full of documents that never touched a scanner: a
spreadsheet of account numbers, a letter written in Word, a CSV exported from a
bank. They carry exactly the information the archive exists to find, and until
now they were skipped as unsupported — which meant the one format most likely to
hold a plain list of everything you own was the format Bindery could not read.

They are converted to PDF and then travel the ordinary path. That is the whole
design: one pipeline, one artifact type downstream, and a Word document ends up
searchable, citable, exportable and viewable exactly like a scan. Handling each
format natively would mean a second implementation of paging, viewing and
citation for every one of them.

The **original is never replaced.** The PDF is a derived artifact alongside the
blob, like the OCR text and the page renders. Exporting still hands back the
.xlsx you put in.
"""

import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path

from worker import subprocess_util

log = logging.getLogger("bindery.worker.convert")

# What LibreOffice is asked to render. Deliberately a closed list of formats
# that hold documents rather than everything it can technically open: source
# code, config and markup are not archive material and would bury the things
# that are.
CONVERTIBLE = {
    # Word processing
    ".doc", ".docx", ".odt", ".rtf",
    # Spreadsheets — the bank export, the inventory, the account list
    ".xls", ".xlsx", ".ods", ".csv",
    # Presentations
    ".ppt", ".pptx", ".odp",
    # Plain text and notes
    ".txt", ".md",
    # Saved web pages. `.mht`/`.mhtml` are a whole page in one file, which is
    # how a browser saves a statement you cannot download as a PDF.
    ".html", ".htm", ".mht", ".mhtml",
    # Apple iWork, via libetonyek. Note that `.pages` files often carry no
    # embedded preview PDF — the ones audited here did not — so extracting a
    # preview is not a shortcut and the real import filter is what does it.
    ".pages", ".numbers", ".key",
}

# Conversion is a cold start of an office suite, not a filter. Generous, but
# bounded: a hung soffice must not hold a worker slot forever.
CONVERT_TIMEOUT_SECONDS = 300


def is_convertible(filename: str | None, mime_type: str | None = None) -> bool:
    if filename and Path(filename).suffix.lower() in CONVERTIBLE:
        return True
    # Some browsers send a useful content type and a useless filename.
    return (mime_type or "") in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "text/csv",
    }


def _filter_for(suffix: str) -> str:
    """Export options, where the defaults produce something unusable.

    A wide CSV rendered at the default page size loses its right-hand columns
    off the edge — which for a bank export is precisely the part with the
    numbers in it. Calc's PDF export scales to fit when told to.
    """
    if suffix in {".csv", ".xls", ".xlsx", ".ods"}:
        return "pdf:calc_pdf_Export"
    return "pdf"


async def to_pdf(source: Path, workdir: Path, *, original_name: str | None = None) -> Path:
    """Render an office document to PDF. Returns the new file's path.

    Each call gets its own LibreOffice profile directory. Without that, two
    conversions running at once quietly fight over one profile and the second
    exits successfully having produced nothing.
    """
    workdir.mkdir(parents=True, exist_ok=True)

    # soffice derives the output name from the input, so the input needs a
    # correct extension — content-addressed blobs have none.
    suffix = Path(original_name or source.name).suffix.lower() or ".bin"
    staged = workdir / f"input{suffix}"
    if staged.resolve() != source.resolve():
        shutil.copy2(source, staged)

    profile = workdir / "lo-profile"
    argv = [
        "soffice",
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to", _filter_for(suffix),
        "--outdir", str(workdir),
        str(staged),
    ]

    await subprocess_util.run(
        argv,
        timeout=CONVERT_TIMEOUT_SECONDS,
        # HOME must be writable: LibreOffice writes there regardless of the
        # profile flag, and the container runs with a read-only-ish home.
        env={**os.environ, "HOME": str(workdir)},
    )

    produced = workdir / f"{staged.stem}.pdf"
    if not produced.is_file() or produced.stat().st_size == 0:
        # soffice exits 0 on several failures, so the output file is the only
        # trustworthy signal that anything happened.
        raise RuntimeError(
            f"LibreOffice reported success but produced no PDF for {suffix} "
            f"({original_name or source.name})"
        )

    log.info(
        "converted %s to PDF (%s bytes)", original_name or source.name,
        produced.stat().st_size,
    )
    return produced


def scratch_dir(root: Path) -> Path:
    return root / f"convert-{uuid.uuid4().hex[:12]}"


async def warm_up() -> None:
    """Pay LibreOffice's cold start once, at worker boot.

    The first conversion in a process takes several seconds longer than the
    rest while the suite builds its profile. Doing it up front means the first
    real document is not the one that waits.
    """
    try:
        await asyncio.wait_for(
            subprocess_util.run(["soffice", "--headless", "--version"], timeout=60),
            timeout=90,
        )
        log.info("office converter ready")
    except Exception as error:
        # Not fatal: everything except office formats still works, and a
        # conversion attempt will report the real problem in context.
        log.warning("office converter did not warm up: %s", error)
