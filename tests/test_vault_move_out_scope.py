"""Moving out of the vault asks the same questions every other route asks (CR-074).

`DELETE /api/vault/items/{document_id}` writes a decrypted original back onto
disk. Its only check used to be `VaultItem.vault_id == vault.id` — a real check,
but the only one — and it then loaded the document and its file with a bare
`select(...).where(id == ...)`, no library filter, no `superseded_at IS NULL`,
and dereferenced the result without a null check.

Two states reach it, and in both the consequence is plaintext written back
under a row that should not have it:

- the document was moved to a library the caller is no longer in
  (`api/moves.py` rewrites `library_id` on the file and its documents together);
- the document was superseded by a re-segmentation, so it is history.

Both answer **404**, not 403 — a 403 confirms the thing exists (ADR-005).

The absent-row case is not exercised here because it is not reachable:
`vault_item.document_id` carries a foreign key to `document.id`, so a
`VaultItem` cannot outlive its document. The null check stays as the thing that
makes that structural rather than incidental.
"""

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from api.db.enums import IngestSource, ReviewState, SourceFileState
from api.db.models import Document, Membership, Page, SourceFile, VaultItem
from api.storage.blobs import blob_path
from api.vault import service, store

ORIGINAL = b"%PDF-1.7\nthe deed to the house\n" + bytes(range(256)) * 40


@pytest.fixture
async def sealed(session, signed_in, tmp_path, monkeypatch):
    """A document in the vault, its plaintext gone from disk."""
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    user, library = await signed_in()
    # Distinct bytes per test: every `signed_in()` makes a fresh library, and a
    # shared constant would put one blob in several at once — the cross-library
    # sharing `seal` refuses.
    original = ORIGINAL + uuid.uuid4().bytes
    digest = hashlib.sha256(original).hexdigest()
    path = blob_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)

    source = SourceFile(
        library_id=library.id, sha256=digest, byte_size=len(original),
        original_filename="deed.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    session.add(Page(source_file_id=source.id, page_number=1, text="conveyance"))
    document = Document(
        library_id=library.id, source_file_id=source.id, page_start=1, page_end=1,
        title="Deed — 14 Brackenridge Road", review_state=ReviewState.FILED,
    )
    session.add(document)
    await session.flush()

    vault = await service.create(session, user.id, "a-long-enough-passphrase", "481516")
    await session.commit()
    key = service.require_key(user.id)

    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()
    assert not blob_path(source.sha256).exists()
    return user, library, document, source, vault, key, original


async def test_a_superseded_document_cannot_be_moved_out(client, session, sealed):
    """A re-segmentation retires the vaulted row; restoring a plaintext under
    it puts bytes back beneath a row nothing reads."""
    _user, _library, document, source, _vault, _key, _original = sealed
    document.superseded_at = datetime.now(UTC)
    await session.commit()

    response = await client.delete(f"/api/vault/items/{document.id}")

    assert response.status_code == 404, response.text
    assert not blob_path(source.sha256).exists(), (
        "the plaintext was written back under a superseded document"
    )
    assert (
        await session.execute(
            sa.select(sa.func.count()).select_from(VaultItem).where(
                VaultItem.document_id == document.id
            )
        )
    ).scalar_one() == 1, "the vault item was released"


async def test_a_document_in_a_library_the_caller_left_cannot_be_moved_out(
    client, session, sealed
):
    """`require_visible` answers 404, so a probe cannot tell "not yours" from
    "not there"."""
    user, library, document, source, _vault, _key, _original = sealed
    await session.execute(
        sa.delete(Membership).where(
            Membership.user_id == user.id, Membership.library_id == library.id
        )
    )
    await session.commit()

    response = await client.delete(f"/api/vault/items/{document.id}")

    assert response.status_code == 404, response.text
    assert not blob_path(source.sha256).exists(), (
        "a decrypted original was written into a library the caller is not in"
    )


async def test_the_ordinary_move_out_still_works(client, session, sealed):
    """The guard above must refuse the two bad states and nothing else."""
    _user, _library, document, source, _vault, _key, original = sealed

    response = await client.delete(f"/api/vault/items/{document.id}")

    assert response.status_code == 200, response.text
    assert blob_path(source.sha256).read_bytes() == original
