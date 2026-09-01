"""Watched-folder ingest (REQ-002, REQ-003).

`/data/inbox/<library>/` — one subdirectory per library, which is also how a
scanner picks a destination: it writes to a share, and the folder name decides
where the document lands.

> The single most likely source of corrupted ingests, and the cheapest to
> prevent: a scanner writing a 40 MB duplex batch over SMB produces a file that
> *exists* but is incomplete for several seconds.

So a file is only picked up once its size and mtime are unchanged across two
consecutive polls. Polling — rather than inotify — because the stability check
needs two observations anyway, and a five-second pickup latency is irrelevant to
a scanner that took a minute to feed.

Ingested files are **moved, never deleted** (invariant 3): they land in
`<inbox>/.ingested/`, and clearing that out is a human's decision.
"""

import asyncio
import contextlib
import logging
import shutil
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import ingest
from api.config import get_settings
from api.db.enums import ActorType, IngestSource
from api.db.models import Library
from api.db.session import SessionFactory
from api.storage.blobs import CHUNK_SIZE, store_stream
from worker import media

log = logging.getLogger("bindery.worker.inbox")

POLL_SECONDS = 5.0
INGESTED_DIR = ".ingested"
FAILED_DIR = ".failed"
# Directories the watcher manages itself, plus anything hidden.
RESERVED = {INGESTED_DIR, FAILED_DIR}

SUPPORTED_SUFFIXES = (
    {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif"} | media.VIDEO_SUFFIXES
)


@dataclass(frozen=True)
class _Observation:
    size: int
    mtime_ns: int


def _slug(value: str) -> str:
    """Fold a library name to a directory-safe form, for matching only."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return "".join(c if c.isalnum() else "-" for c in normalized.lower()).strip("-")


async def _libraries_by_directory(session: AsyncSession) -> dict[str, Library]:
    """Map an inbox subdirectory name to a library.

    Both the slugged name and the raw id are accepted, so `inbox/household/` and
    `inbox/<uuid>/` both work.
    """
    libraries = (await session.execute(sa.select(Library))).scalars().all()
    mapping: dict[str, Library] = {}
    for library in libraries:
        mapping[_slug(library.name)] = library
        mapping[str(library.id)] = library
    return mapping


def _candidates(inbox: Path) -> list[Path]:
    files: list[Path] = []
    for library_dir in sorted(p for p in inbox.iterdir() if p.is_dir()):
        if library_dir.name.startswith("."):
            continue
        for path in sorted(library_dir.iterdir()):
            if path.is_dir() or path.name.startswith(".") or path.name in RESERVED:
                continue
            files.append(path)
    return files


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            yield chunk


def _retire(path: Path, subdirectory: str) -> Path:
    """Move a handled file out of the watch path without destroying it."""
    destination_dir = path.parent / subdirectory
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / path.name
    suffix = 1
    while destination.exists():
        destination = destination_dir / f"{path.stem}.{suffix}{path.suffix}"
        suffix += 1
    shutil.move(str(path), str(destination))
    return destination


async def _ingest_file(path: Path, library: Library) -> None:
    blob = await store_stream(_file_chunks(path))
    async with SessionFactory() as session:
        result = await ingest.register(
            session,
            blob,
            library_id=library.id,
            ingest_source=IngestSource.WATCHED_FOLDER,
            original_filename=path.name,
            mime_type=None,
            actor_type=ActorType.SYSTEM,
            metadata={"inbox_path": str(path), "library_dir": path.parent.name},
        )
        await session.commit()

    if result.duplicate:
        log.info("%s is already in the archive (%s); moved aside", path.name, blob.sha256[:12])
    else:
        log.info("ingested %s into %s as %s", path.name, library.name, blob.sha256[:12])
    _retire(path, INGESTED_DIR)


async def watch_inbox(stopping: asyncio.Event) -> None:
    """Poll the inbox until told to stop.

    Never raises: a watcher that dies takes the scanner path down silently, which
    is exactly the failure invariant 8 exists to prevent.
    """
    inbox = get_settings().inbox_root
    seen: dict[Path, _Observation] = {}

    log.info("watching %s", inbox)
    while not stopping.is_set():
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            async with SessionFactory() as session:
                libraries = await _libraries_by_directory(session)

            current: dict[Path, _Observation] = {}
            for path in _candidates(inbox):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue  # moved or removed between listing and stat
                current[path] = _Observation(stat.st_size, stat.st_mtime_ns)

            for path, observation in current.items():
                previous = seen.get(path)
                if previous is None or previous != observation:
                    # Still being written, or seen for the first time. Wait for a
                    # second identical observation before touching it (REQ-003).
                    continue

                library = libraries.get(_slug(path.parent.name))
                if library is None:
                    log.warning(
                        "no library matches inbox directory %r; leaving %s in place",
                        path.parent.name, path.name,
                    )
                    continue

                if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    log.warning("unsupported file type %s; moved to %s", path.name, FAILED_DIR)
                    _retire(path, FAILED_DIR)
                    continue

                if observation.size == 0:
                    log.warning("%s is empty; moved to %s", path.name, FAILED_DIR)
                    _retire(path, FAILED_DIR)
                    continue

                try:
                    await _ingest_file(path, library)
                except Exception:
                    log.exception("failed to ingest %s", path)
                    with contextlib.suppress(OSError):
                        _retire(path, FAILED_DIR)

            seen = {path: obs for path, obs in current.items() if path.exists()}
        except Exception:
            log.exception("inbox scan failed; continuing")

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=POLL_SECONDS)

    log.info("inbox watcher stopped")
