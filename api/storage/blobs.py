"""Content-addressed blob store.

Invariant 1: originals are never modified. Bytes land at
``/data/blobs/<aa>/<bb>/<sha256>``, are made read-only on write, and are never
opened for writing again. The content address is also what makes exact
deduplication free — the same bytes always resolve to the same path.
"""

import hashlib
import os
import stat
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from api.config import get_settings

CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class BlobWriteResult:
    sha256: str
    byte_size: int
    path: Path
    # False when the content address already existed, i.e. these exact bytes are
    # already in the archive.
    is_new: bool


def blob_path(sha256: str) -> Path:
    root = get_settings().blob_root
    return root / sha256[:2] / sha256[2:4] / sha256


async def store_stream(chunks: AsyncIterator[bytes]) -> BlobWriteResult:
    """Stream bytes to disk, hashing as we go, then move into place.

    Hashing while streaming means a multi-gigabyte scanner batch is never held
    in memory and is never read twice.
    """
    settings = get_settings()
    settings.temp_root.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    byte_size = 0

    fd, temp_name = tempfile.mkstemp(dir=settings.temp_root, prefix="upload-")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                byte_size += len(chunk)

        sha256 = digest.hexdigest()
        destination = blob_path(sha256)

        if destination.exists():
            # Same content address, so the stored bytes are already these bytes.
            # Drop the temp copy; the blob itself is untouched.
            os.unlink(temp_path)
            return BlobWriteResult(sha256, byte_size, destination, is_new=False)

        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, destination)
        # Read-only for everyone: write-once, enforced by the filesystem.
        os.chmod(destination, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return BlobWriteResult(sha256, byte_size, destination, is_new=True)
    except BaseException:
        if temp_path.exists():
            os.unlink(temp_path)
        raise
