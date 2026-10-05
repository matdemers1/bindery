"""The account purge cannot unlink a blob an ingest of the same bytes is relying on (BND-T-23.5).

Blobs are content-addressed and shared (migration 0017), so the purge's question — "does any row
still hold this hash?" — is only true until the next upload of the same bytes. The race it lost:

1. an upload of identical bytes streams them, finds the blob already on disk and drops its copy;
2. the purge asks the question, and the upload's row is not committed yet, so the answer is no;
3. the purge unlinks the blob;
4. the upload commits a `source_file` row pointing at a file that is gone.

Step 4 is the one outcome the purge's ordering rule exists to prevent (BND-ADR-015). The fix is a
Postgres advisory lock keyed on the hash: every ingest holds it *shared* from before it looks for
an existing blob until its transaction ends, and the purge takes it *exclusive*, asks again under
it, and unlinks before letting go. These tests interleave the two on real connections, in both
orders, and check the lock is what made the difference by looking for it waiting in `pg_locks`.
"""

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa

from api import account_purge, ingest
from api.db.enums import ActorType, IngestSource
from api.db.models import SourceFile
from api.db.session import SessionFactory
from api.storage import blobs

# Long enough for a task that is going to block to have reached the lock; the assertions that
# follow ask Postgres whether it is waiting rather than trusting the clock.
SETTLE = 0.3


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _payload() -> tuple[bytes, str]:
    data = f"%PDF-1.4 a deed {uuid.uuid4()}".encode()
    return data, hashlib.sha256(data).hexdigest()


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _on_disk(data: bytes, sha: str):
    """The blob as a purged account left it: on disk, with no row holding it any more."""
    path = blobs.blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _waiting_on_the_blob_lock(sha: str) -> int:
    """How many backends are queued on this content address's advisory lock right now."""
    async with SessionFactory() as probe:
        return await probe.scalar(
            sa.text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                "AND classid = :namespace AND objid = :key AND objsubid = 2"
            ),
            {
                # pg_locks reports the two int4 halves as unsigned oids.
                "namespace": blobs.LOCK_NAMESPACE % (1 << 32),
                "key": blobs._lock_key(sha) % (1 << 32),
            },
        )


async def _register(session, blob, library_id: uuid.UUID) -> None:
    await ingest.register(
        session,
        blob,
        library_id=library_id,
        ingest_source=IngestSource.WEB_UPLOAD,
        original_filename="deed.pdf",
        mime_type="application/pdf",
        actor_type=ActorType.SYSTEM,
    )


async def test_an_upload_that_found_the_blob_makes_the_purge_wait_and_keep_it(
    user_factory, data_root
) -> None:
    """The race as it happened: the upload finds the blob first, the purge asks second."""
    _, library = await user_factory()
    data, sha = _payload()
    path = _on_disk(data, sha)

    async with SessionFactory() as upload, SessionFactory() as purge:
        # Step 1: the upload streams the same bytes and finds the blob already there.
        blob = await blobs.store_stream(_chunks(data), session=upload)
        assert blob.sha256 == sha and not blob.is_new

        # Step 2: the purge reaches the blob before the upload's row is committed.
        release = asyncio.create_task(account_purge._remove_files(purge, _purged(sha)))
        await asyncio.sleep(SETTLE)
        assert not release.done(), "the purge did not wait for the upload holding the hash"
        assert await _waiting_on_the_blob_lock(sha) == 1, (
            "the purge is not queued on the content address's advisory lock"
        )
        assert path.exists()

        # Step 4 lands before step 3 can: the upload's row commits, which lets the purge go.
        await _register(upload, blob, library.id)
        await upload.commit()
        await asyncio.wait_for(release, timeout=10)

    assert path.exists(), "the purge unlinked a blob an upload had just committed a row for"
    assert path.read_bytes() == data


async def test_a_purge_holding_the_lock_makes_the_upload_wait_and_write_the_blob_again(
    user_factory, session, data_root
) -> None:
    """The other order: the purge decides first, so the upload has to bring its own bytes."""
    _, library = await user_factory()
    library_id = library.id
    data, sha = _payload()
    path = _on_disk(data, sha)

    async with SessionFactory() as purge, SessionFactory() as upload:
        # The purge takes the lock, finds nothing holding the hash, and unlinks — and has not
        # ended its transaction yet.
        await account_purge._release_blob(purge, sha, set())
        assert not path.exists()

        store = asyncio.create_task(blobs.store_stream(_chunks(data), session=upload))
        await asyncio.sleep(SETTLE)
        assert not store.done(), "the upload looked for the blob while the purge held it"
        assert await _waiting_on_the_blob_lock(sha) == 1

        await purge.rollback()  # the purge's transaction ends, and the lock with it
        blob = await asyncio.wait_for(store, timeout=10)
        # It looked after the unlink, so it wrote the file rather than trusting one that is gone.
        assert blob.is_new
        await _register(upload, blob, library_id)
        await upload.commit()

    assert path.read_bytes() == data
    held = await session.scalar(
        sa.select(sa.func.count()).select_from(SourceFile).where(SourceFile.sha256 == sha)
    )
    assert held == 1


async def test_two_uploads_of_the_same_bytes_never_wait_on_each_other(
    user_factory, data_root
) -> None:
    """The ingest's hold is shared. An import holding several hashes for its whole transaction
    cannot block, or deadlock against, another import of the same files."""
    data, sha = _payload()
    async with SessionFactory() as first, SessionFactory() as second:
        await blobs.store_stream(_chunks(data), session=first)
        again = await asyncio.wait_for(blobs.store_stream(_chunks(data), session=second), timeout=5)
        assert again.sha256 == sha and not again.is_new
        await first.rollback()
        await second.rollback()


async def test_a_blob_nothing_holds_is_still_removed(data_root) -> None:
    """The lock changes when the purge may unlink, not whether it does."""
    data, sha = _payload()
    path = _on_disk(data, sha)
    async with SessionFactory() as purge:
        await account_purge._remove_files(purge, _purged(sha))
    assert not path.exists()


def _purged(sha: str) -> account_purge.Purged:
    purged = account_purge.Purged(user_id=uuid.uuid4())
    purged.blobs[sha] = set()
    return purged


def test_the_lock_key_fits_an_int4_and_is_stable() -> None:
    for sha in ("0" * 64, "f" * 64, "7fffffff" + "0" * 56, "80000000" + "0" * 56):
        key = blobs._lock_key(sha)
        assert -(1 << 31) <= key < (1 << 31)
        assert key == blobs._lock_key(sha)
