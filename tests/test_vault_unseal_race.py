"""A failed unseal never removes or overwrites another library's copy of the same bytes
(BND-T-23.7).

While a document is vaulted its plaintext is gone from the blob store, and another library may
upload the identical file in the meantime, or while the unseal is running. The restore used to
write over the content address and, when the written file did not verify, unlink it, whoever
else had come to rely on it. Now, under `blobs.lock_for_removal`:

- an existing file that verifies is used as it is, never overwritten;
- a restore that does not verify is removed only after asking again whether anybody else holds
  the hash, and an upload of the same bytes waits for that decision rather than trusting a file
  that is about to be judged corrupt.

A corrupt vault object is refused by its AEAD tag before anything is written; a write that comes
out corrupt (the case the unlink exists for) is simulated by replacing the writer, because there
is no honest way to make a disk flip a bit on demand.
"""

import asyncio
import hashlib
import threading
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa

from api import ingest
from api.db.enums import ActorType, IngestSource, ReviewState, SourceFileState
from api.db.models import Document, Page, SourceFile, VaultItem
from api.db.session import SessionFactory
from api.storage import blobs
from api.vault import crypto, service, store

SETTLE = 0.3


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _waiting_on_the_blob_lock(sha: str) -> int:
    async with SessionFactory() as probe:
        return await probe.scalar(
            sa.text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                "AND classid = :namespace AND objid = :key AND objsubid = 2"
            ),
            {
                "namespace": blobs.LOCK_NAMESPACE % (1 << 32),
                "key": blobs._lock_key(sha) % (1 << 32),
            },
        )


def _corrupt_writer(path, payload: bytes) -> None:
    """The restore's write, coming out wrong: the bytes on disk are not the bytes decrypted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes([payload[0] ^ 0xFF]) + payload[1:])


async def _vaulted(session, user_factory):
    """A document sealed into its owner's vault, so its plaintext is gone from the blob store."""
    user, library = await user_factory()
    original = b"%PDF-1.7\na deed that comes back out\n" + uuid.uuid4().bytes
    sha = hashlib.sha256(original).hexdigest()
    path = blobs.blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)
    source = SourceFile(
        library_id=library.id,
        sha256=sha,
        byte_size=len(original),
        original_filename="deed.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    session.add(Page(source_file_id=source.id, page_number=1, text="the deed"))
    document = Document(
        library_id=library.id,
        source_file_id=source.id,
        page_start=1,
        page_end=1,
        title="Deed",
        review_state=ReviewState.FILED,
    )
    session.add(document)
    vault = await service.create(session, user.id, "a-long-enough-passphrase", "481516")
    await session.commit()
    key = service.require_key(user.id)
    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()
    assert not path.exists()
    item = (
        await session.execute(sa.select(VaultItem).where(VaultItem.document_id == document.id))
    ).scalar_one()
    return document, source, item, key, original, path


async def _upload_elsewhere(user_factory, original: bytes) -> None:
    """Another household member uploads the identical file into their own library."""
    _, library = await user_factory()
    async with SessionFactory() as upload:
        blob = await blobs.store_stream(_chunks(original), session=upload)
        await _register(upload, blob, library.id)
        await upload.commit()


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


async def test_an_upload_arriving_during_a_failed_unseal_waits_and_keeps_good_bytes(
    session, user_factory, data_root, monkeypatch
) -> None:
    """The race. The restore writes, the write comes out corrupt, and before the unseal has
    decided to remove it an upload of the same bytes arrives. Unlocked, the upload found the
    corrupt file at its content address, trusted it, and committed a row for it. Locked, it
    waits; the unseal removes its bad write and refuses; the upload then writes good bytes."""
    document, source, item, key, original, path = await _vaulted(session, user_factory)
    _, elsewhere = await user_factory()
    sha, document_id, elsewhere_id = source.sha256, document.id, elsewhere.id

    monkeypatch.setattr(store, "_write_atomically", _corrupt_writer)
    reached, release = threading.Event(), threading.Event()
    real_digest = store._sha256_of

    def held_open(target):
        # The unseal's verify of its own write, held until the upload has had its chance.
        reached.set()
        release.wait(10)
        return real_digest(target)

    monkeypatch.setattr(store, "_sha256_of", held_open)

    unseal = asyncio.create_task(store.unseal(session, document, source, item, key))
    while not reached.is_set():
        await asyncio.sleep(0.01)
    assert path.exists(), "the corrupt write is on disk, waiting to be judged"

    async with SessionFactory() as upload:
        store_task = asyncio.create_task(blobs.store_stream(_chunks(original), session=upload))
        await asyncio.sleep(SETTLE)
        assert not store_task.done(), "the upload looked at the address while the unseal held it"
        assert await _waiting_on_the_blob_lock(sha) == 1, (
            "the upload is not queued on the content address's advisory lock"
        )

        release.set()
        with pytest.raises(store.VaultRefused, match="did not verify"):
            await asyncio.wait_for(unseal, timeout=10)
        await session.rollback()  # what the route does with a refusal; it ends the lock

        blob = await asyncio.wait_for(store_task, timeout=10)
        assert blob.is_new, "the upload trusted a file the unseal was about to remove"
        await _register(upload, blob, elsewhere_id)
        await upload.commit()

    assert path.read_bytes() == original, "the other library's original is not its bytes"
    session.expire_all()
    assert (await session.get(Document, document_id)).vaulted_by is not None


async def test_an_unseal_never_overwrites_a_blob_another_library_holds(
    session, user_factory, data_root, monkeypatch
) -> None:
    """The bytes came back into the archive through somebody else's upload. The restore uses
    that file as it is; the old code wrote over it and, had the write come out wrong, unlinked
    it."""
    document, source, item, key, original, path = await _vaulted(session, user_factory)
    await _upload_elsewhere(user_factory, original)
    inode = path.stat().st_ino

    monkeypatch.setattr(store, "_write_atomically", _corrupt_writer)
    await store.unseal(session, document, source, item, key)
    await session.commit()

    assert path.read_bytes() == original
    assert path.stat().st_ino == inode, "the other library's file was replaced"


async def test_a_corrupt_vault_object_is_refused_before_anything_is_written(
    session, user_factory, data_root
) -> None:
    """A flipped bit in the ciphertext fails its AEAD tag, so the restore never reaches the blob
    store, and the identical file another library uploaded is untouched."""
    document, source, item, key, original, path = await _vaulted(session, user_factory)
    await _upload_elsewhere(user_factory, original)
    vault_object = store.object_path(item.object_name)
    data = bytearray(vault_object.read_bytes())
    data[-1] ^= 0x01
    vault_object.chmod(0o600)
    vault_object.write_bytes(bytes(data))

    with pytest.raises(crypto.WrongSecret):
        await store.unseal(session, document, source, item, key)
    await session.rollback()

    assert path.read_bytes() == original


async def test_a_failed_restore_nobody_else_holds_is_still_removed(
    session, user_factory, data_root, monkeypatch
) -> None:
    """The lock changes when a bad write may be removed, not whether it is."""
    document, source, item, key, _, path = await _vaulted(session, user_factory)
    monkeypatch.setattr(store, "_write_atomically", _corrupt_writer)
    with pytest.raises(store.VaultRefused, match="it has been removed"):
        await store.unseal(session, document, source, item, key)
    await session.rollback()
    assert not path.exists()
