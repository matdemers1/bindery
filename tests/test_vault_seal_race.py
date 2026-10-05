"""Sealing a document cannot destroy another library's copy of the same bytes (BND-T-23.6).

Blobs are content-addressed and shared (migration 0017). `seal` refuses up front when another
library already holds the bytes, but that answer holds only until the next upload of them:

1. the seal checks — nobody else holds these bytes — and starts encrypting;
2. an upload of the identical bytes into another library finds the blob already on disk;
3. the seal unlinks the plaintext;
4. the upload commits a `source_file` row pointing at a file that is gone.

The fix is the account purge's (BND-T-23.5): the plaintext is removed only under
`blobs.lock_for_removal`, after asking again whether any row outside the vault holds the hash.
The upload holds the shared half of that lock from before it looks for the blob until its row
commits, so the seal waits for it and then sees it. Real connections, and the waiter is read
from `pg_locks` rather than inferred from timing.
"""

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa

from api import ingest
from api.db.enums import ActorType, IngestSource, ReviewState, SourceFileState
from api.db.models import Document, Page, SourceFile, VaultItem
from api.db.session import SessionFactory
from api.storage import blobs
from api.vault import service, store

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


async def _sealable(session, user_factory):
    """A filed document whose original is on disk, its owner's open vault, and the key."""
    user, library = await user_factory()
    original = b"%PDF-1.7\na deed somebody else also has\n" + uuid.uuid4().bytes
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
    return user, document, source, vault, service.require_key(user.id), original, path


async def test_an_upload_that_found_the_blob_during_a_seal_keeps_its_file(
    session, user_factory, data_root
) -> None:
    user, document, source, vault, key, original, path = await _sealable(session, user_factory)
    document_id, sha, user_id = document.id, source.sha256, user.id
    _, elsewhere = await user_factory()

    async with SessionFactory() as upload:
        # The seal's up-front check has nothing to refuse: nobody else holds these bytes yet.
        # Then somebody uploads the identical file into their own library, and it finds the
        # blob already on disk.
        blob = await blobs.store_stream(_chunks(original), session=upload)
        assert blob.sha256 == sha and not blob.is_new

        seal = asyncio.create_task(store.seal(session, document, source, vault.id, user.id, key))
        await asyncio.sleep(SETTLE)
        assert not seal.done(), "the seal removed its plaintext without waiting for the upload"
        assert await _waiting_on_the_blob_lock(sha) == 1, (
            "the seal is not queued on the content address's advisory lock"
        )
        assert path.exists()

        await ingest.register(
            upload,
            blob,
            library_id=elsewhere.id,
            ingest_source=IngestSource.WEB_UPLOAD,
            original_filename="deed.pdf",
            mime_type="application/pdf",
            actor_type=ActorType.SYSTEM,
        )
        await upload.commit()
        sealed = await asyncio.wait_for(seal, timeout=10)

    assert path.exists(), "the seal unlinked a blob another library had just committed a row for"
    assert path.read_bytes() == original
    assert any("added elsewhere" in warning for warning in sealed.warnings), sealed.warnings
    # And the seal itself still happened: the document is in the vault, encrypted.
    session.expire_all()
    assert (await session.get(Document, document_id)).vaulted_by == user_id
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(VaultItem)
            .where(VaultItem.document_id == document_id)
        )
        == 1
    )


async def test_a_seal_with_nobody_else_holding_the_bytes_still_removes_the_plaintext(
    session, user_factory, data_root
) -> None:
    """The lock changes when the plaintext may go, not whether it does. The row just sealed
    holds the hash too, and must not count as somebody else."""
    user, document, source, vault, key, _, path = await _sealable(session, user_factory)
    sealed = await store.seal(session, document, source, vault.id, user.id, key)
    assert not path.exists()
    assert sealed.warnings == [], sealed.warnings
