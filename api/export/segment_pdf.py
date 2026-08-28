"""Exporting a segment as a standalone PDF (REQ-042).

> Exports are derivative artifacts, never mutations.

The source blob is opened read-only, the segment's pages are copied into a new
file under `/data/exports/`, and the original is closed untouched. The export is
content-addressed by the source hash plus the page range, so asking twice costs
nothing and the same segment always resolves to the same file.
"""

import logging
from pathlib import Path

import pikepdf

from api.artifacts import derived_for
from api.config import get_settings
from api.storage.blobs import blob_path

log = logging.getLogger("bindery.export")


def export_root() -> Path:
    return get_settings().data_root / "exports"


def export_path(sha256: str, page_start: int, page_end: int) -> Path:
    return export_root() / sha256[:2] / f"{sha256}-p{page_start}-{page_end}.pdf"


def export_segment(sha256: str, page_start: int, page_end: int) -> Path:
    """Write the segment's pages to a standalone PDF and return its path.

    Prefers the normalized PDF so the export carries the searchable text layer;
    falls back to the original for a file that has not been through OCR yet.
    """
    destination = export_path(sha256, page_start, page_end)
    if destination.is_file():
        return destination

    normalized = derived_for(sha256).normalized_pdf
    source = normalized if normalized.is_file() else blob_path(sha256)
    if not source.is_file():
        raise FileNotFoundError(f"no PDF available for {sha256}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".partial")

    with pikepdf.open(source) as original:
        total = len(original.pages)
        if page_start < 1 or page_end > total:
            raise ValueError(
                f"pages {page_start}-{page_end} fall outside the file's 1-{total}"
            )
        with pikepdf.Pdf.new() as extracted:
            # pikepdf pages are 0-indexed; the archive is 1-indexed everywhere a
            # human can see it.
            for number in range(page_start - 1, page_end):
                extracted.pages.append(original.pages[number])
            extracted.save(temporary)

    # Rename last, so an interrupted export never leaves a truncated PDF behind
    # under the name a caller would trust.
    temporary.replace(destination)
    log.info("exported %s pages %s-%s", sha256[:12], page_start, page_end)
    return destination
