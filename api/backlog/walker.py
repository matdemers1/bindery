"""Walking a directory tree that was not built for us.

A real backlog directory is twenty years of accretion: symlink loops, permission
holes, `.DS_Store`, resource forks, files being written right now, and the
occasional 4 GB scan nobody meant to keep.

**The walk never aborts.** It records what it could not read and keeps going,
because failing a five-thousand-file import on file 3,000 because one directory
was unreadable is how an import becomes a thing you never finish (R-03).
"""

import hashlib
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from api.vault.store import VIDEO_SUFFIXES

log = logging.getLogger("bindery.import.walk")

# Scans and photographs. All of these reach OCR directly — verified against
# ocrmypdf rather than assumed, including the less common raster formats a
# phone or a screenshot tool produces.
SCANNED = {
    ".pdf",
    ".jpg", ".jpeg", ".png", ".tif", ".tiff",
    ".heic", ".heif",
    ".gif", ".webp", ".bmp",
}

# Documents that never touched a scanner. A spreadsheet of account numbers is
# exactly the sort of thing an archive is for, and these were being skipped as
# unsupported. Converted to PDF on the way in — see `worker/convert.py`.
OFFICE = {
    ".doc", ".docx", ".odt", ".rtf",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
    ".txt", ".md",
    # Saved web pages: an order confirmation, a pay statement printed to file.
    # Ordinary archive material, and the reason a whole `.mht` bank statement
    # was previously unreachable.
    ".html", ".htm", ".mht", ".mhtml",
    # Apple iWork. LibreOffice reads these through libetonyek.
    ".pages", ".numbers", ".key",
}

# Videos (Phase 18). Not through the pipeline — there is nothing to OCR — but
# stored, described by their own metadata, and playable. The list lives on the
# vault module because api/ may not import worker/, and a test asserts the
# worker's copy agrees.
VIDEO = set(VIDEO_SUFFIXES)

SUPPORTED = SCANNED | OFFICE | VIDEO

# Deliberately absent, after auditing a real 80,000-file tree:
#
#   .psd            design sources, not documents
#   .one .onetoc2   OneNote — no converter exists that works; tested, it fails
#   .zip .gz .rar   archives; unpacking is a separate decision with its own
#                   hazards (nesting, bombs, and what "the original" then means)
#   .mca .gcode .stl .3mf .class .jar .js .java .swift .plist .json .xml …
#                   code, build output, 3D printing and game data
#   .exe .ipa .wav .mp4
#
# The `skipped_unsupported` counter is the honest report of all of it.
# Directories that are never documents.
SKIP_DIRS = {".git", ".svn", "node_modules", "__pycache__", ".Trash", "$RECYCLE.BIN",
             ".Spotlight-V100", ".fseventsd", ".TemporaryItems", "@eaDir"}
CHUNK = 1024 * 1024
# Anything larger is almost certainly not a document someone scanned.
MAX_BYTES = 2 * 1024 * 1024 * 1024


@dataclass
class WalkResult:
    files: list[Path] = field(default_factory=list)
    # (path, why) — surfaced to the user rather than swallowed.
    errors: list[tuple[str, str]] = field(default_factory=list)
    skipped_unsupported: int = 0
    skipped_hidden: int = 0
    skipped_too_large: int = 0
    total_bytes: int = 0


def walk(root: Path, *, follow_symlinks: bool = False) -> WalkResult:
    result = WalkResult()
    seen_dirs: set[tuple[int, int]] = set()

    for current, dirnames, filenames in os.walk(root, followlinks=follow_symlinks,
                                                onerror=lambda e: result.errors.append(
                                                    (str(getattr(e, "filename", root)), str(e)))):
        directory = Path(current)

        # A symlinked directory that leads back to an ancestor makes os.walk run
        # for ever. Identity is (device, inode), which survives path trickery.
        try:
            stat = directory.stat()
            key = (stat.st_dev, stat.st_ino)
            if key in seen_dirs:
                dirnames[:] = []
                continue
            seen_dirs.add(key)
        except OSError as exc:
            result.errors.append((str(directory), str(exc)))
            dirnames[:] = []
            continue

        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]

        for name in filenames:
            path = directory / name
            if name.startswith("."):
                result.skipped_hidden += 1
                continue
            if path.suffix.lower() not in SUPPORTED:
                result.skipped_unsupported += 1
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                result.errors.append((str(path), str(exc)))
                continue
            if size == 0:
                result.errors.append((str(path), "empty file"))
                continue
            if size > MAX_BYTES:
                result.skipped_too_large += 1
                continue

            result.files.append(path)
            result.total_bytes += size

    result.files.sort()
    return result


def hash_file(path: Path) -> str | None:
    """Content address, for duplicate detection before anything is copied."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
    except OSError as exc:
        log.warning("cannot hash %s: %s", path, exc)
        return None
    return digest.hexdigest()


def iter_hashed(paths: list[Path]) -> Iterator[tuple[Path, str | None, int]]:
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        yield path, hash_file(path), size
