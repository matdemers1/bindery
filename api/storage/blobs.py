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

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings

CHUNK_SIZE = 1024 * 1024

# The namespace half of the two-int advisory lock key a content address is held
# under, so this lock cannot collide with `quota`'s (which uses its own) by
# accident. "BLOB" in ASCII.
LOCK_NAMESPACE = 0x424C4F42


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


def _lock_key(sha256: str) -> int:
    """The int4 half of the lock key: the first 32 bits of the hash, signed.

    A collision costs two unrelated hashes a moment of waiting on each other and
    nothing else, which is why truncating is safe here.
    """
    value = int(sha256[:8], 16)
    return value - (1 << 32) if value >= (1 << 31) else value


def _lock(function: str, sha256: str) -> sa.Select:
    return sa.select(
        getattr(sa.func, function)(
            sa.literal(LOCK_NAMESPACE, sa.Integer),
            sa.literal(_lock_key(sha256), sa.Integer),
        )
    )


async def hold(session: AsyncSession, sha256: str) -> None:
    """Hold this content address against removal until the transaction ends.

    Taken by every ingest *before* it decides whether the blob already exists,
    and held until the `source_file` row that will point at it is committed (or
    the transaction is abandoned). Shared, so two uploads of the same bytes
    never wait on each other — only `lock_for_removal` waits on this.

    Without it the account purge (BND-ADR-015) could ask "does any row still
    hold this hash?", hear no, and unlink the blob in the moment between an
    upload of identical bytes finding the file already on disk and committing
    the row that relies on it — a row pointing at a file that is gone, which is
    the one outcome the purge's ordering rule exists to prevent (BND-T-23.5).
    """
    await session.execute(_lock("pg_advisory_xact_lock_shared", sha256))


async def lock_for_removal(session: AsyncSession, sha256: str) -> None:
    """Exclusive: wait out every ingest holding this content address, and keep
    new ones out, until the transaction ends.

    The caller re-asks whether any row holds the hash *after* this returns, and
    unlinks only before it ends its transaction. Under READ COMMITTED each
    statement takes a fresh snapshot, so the question sees every row an ingest
    committed before releasing its hold.
    """
    await session.execute(_lock("pg_advisory_xact_lock", sha256))


async def store_stream(chunks: AsyncIterator[bytes], *, session: AsyncSession) -> BlobWriteResult:
    """Stream bytes to disk, hashing as we go, then move into place.

    Hashing while streaming means a multi-gigabyte scanner batch is never held
    in memory and is never read twice.

    `session` is the transaction that will record the file. The content address
    is held in it (`hold`) before the store looks for an existing blob, and
    stays held until that transaction ends — so the blob this returns cannot be
    removed before the row that points at it is committed. Required rather than
    optional, so a new ingest door cannot forget it.
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
        # Before looking: a blob seen to exist must still exist when the row
        # pointing at it lands.
        await hold(session, sha256)
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
