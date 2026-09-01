"""Moving documents in and out (T-16.5, T-16.6, REQ-181, REQ-182).

The one irreversible operation in this project. Every test here is really the
same question asked from a different angle: **can the bytes come back?**
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api.db.enums import (
    IngestSource,
    JobStage,
    JobState,
    ReviewState,
    SourceFileState,
    TagSource,
)
from api.db.models import Document, DocumentTag, Job, Page, SourceFile, Tag, VaultItem
from api.storage.blobs import blob_path
from api.vault import crypto, service, store

ORIGINAL = b"%PDF-1.7\nthe deed to the house\n" + bytes(range(256)) * 40
PHRASE = "brackenridge conveyance"


@pytest.fixture
async def ready(session, signed_in, tmp_path, monkeypatch):
    """A real document on disk, a vault, and the key in hand."""
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    user, library = await signed_in()
    digest = hashlib.sha256(ORIGINAL).hexdigest()
    path = blob_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ORIGINAL)

    source = SourceFile(
        library_id=library.id, sha256=digest, byte_size=len(ORIGINAL),
        original_filename="deed.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=2, state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    for number in (1, 2):
        session.add(
            Page(source_file_id=source.id, page_number=number,
                 text=f"page {number}: {PHRASE}")
        )
    document = Document(
        library_id=library.id, source_file_id=source.id, page_start=1, page_end=2,
        title="Deed — 14 Brackenridge Road", review_state=ReviewState.FILED,
    )
    session.add(document)
    await session.flush()

    tag = Tag(library_id=library.id, name="property", slug="property")
    session.add(tag)
    await session.flush()
    session.add(
        DocumentTag(document_id=document.id, tag_id=tag.id, source=TagSource.HUMAN)
    )

    vault = await service.create(session, user.id, "a-long-enough-passphrase", "481516")
    await session.commit()
    key = service.require_key(user.id)
    return user, library, document, source, vault, key


async def test_the_bytes_come_back_exactly(session, ready):
    """The whole feature, in one assertion."""
    user, _, document, source, vault, key = ready
    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()

    item = (
        await session.execute(
            sa.select(VaultItem).where(VaultItem.document_id == document.id)
        )
    ).scalar_one()
    assert store.open_object(item.object_name, document.id, key) == ORIGINAL


async def test_the_plaintext_is_gone_from_disk(session, ready):
    user, _, document, source, vault, key = ready
    path = blob_path(source.sha256)
    assert path.is_file()

    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()
    assert not path.exists(), "the original is still on disk; this is not a vault"


async def test_the_vault_object_is_not_named_after_the_content(session, ready):
    """A content address is an existence oracle: anyone holding a copy of the
    file could confirm the archive holds it, without decrypting anything."""
    user, _, document, source, vault, key = ready
    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()

    item = (
        await session.execute(
            sa.select(VaultItem).where(VaultItem.document_id == document.id)
        )
    ).scalar_one()
    assert source.sha256 not in item.object_name
    assert item.object_name != source.sha256


async def test_nothing_is_deleted_when_the_ciphertext_does_not_verify(
    session, ready, monkeypatch
):
    """The ordering that separates a vault from a shredder.

    Encryption is sabotaged so the read-back cannot match. The original must
    still be on disk and the document must be untouched.
    """
    user, _, document, source, vault, key = ready
    monkeypatch.setattr(store.crypto, "encrypt", lambda *a, **k: b"nonsense" * 8)

    with pytest.raises(store.VaultRefused, match="did not read back"):
        await store.seal(session, document, source, vault.id, user.id, key)

    assert blob_path(source.sha256).is_file(), "the original was deleted anyway"
    assert document.vaulted_by is None
    assert document.title == "Deed — 14 Brackenridge Road"
    # And the object it wrote before discovering the problem is cleaned up,
    # rather than accumulating in the vault as undecryptable garbage.
    assert list(store.vault_root().rglob("*")) == [] or not any(
        path.is_file() for path in store.vault_root().rglob("*")
    )


async def test_the_page_text_leaves_the_searchable_table(session, ready):
    """Leaving it in `page` would leak the document while locked, through
    snippets, hit counts and facets."""
    user, _, document, source, vault, key = ready
    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()

    remaining = (
        await session.execute(
            sa.select(sa.func.count(Page.id)).where(Page.source_file_id == source.id)
        )
    ).scalar_one()
    assert remaining == 0


async def test_the_title_leaves_the_row(session, ready):
    """A locked archive that still knows what a document is called knows most
    of what a title is for."""
    user, _, document, source, vault, key = ready
    await store.seal(session, document, source, vault.id, user.id, key)
    assert document.title is None


async def test_tags_are_revoked_rather_than_carried(session, ready):
    """A tag is a library-wide row. Leaving the link would let anyone browsing
    tags see that *something* tagged "property" exists and is unreachable."""
    user, _, document, source, vault, key = ready
    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()

    live_links = (
        await session.execute(
            sa.select(sa.func.count(DocumentTag.document_id)).where(
                DocumentTag.document_id == document.id, DocumentTag.removed_at.is_(None)
            )
        )
    ).scalar_one()
    assert live_links == 0
    # Revoked, not deleted — the same pattern a cross-library move uses, so the
    # history of what it was tagged survives.
    total = (
        await session.execute(
            sa.select(sa.func.count(DocumentTag.document_id)).where(
                DocumentTag.document_id == document.id
            )
        )
    ).scalar_one()
    assert total == 1


async def test_it_refuses_while_the_pipeline_is_still_working(session, ready):
    """Sealing a half-read document leaves jobs pointing at bytes that no
    longer exist."""
    user, _, document, source, vault, key = ready
    session.add(
        Job(source_file_id=source.id, stage=JobStage.NORMALIZE, state=JobState.QUEUED)
    )
    await session.commit()

    with pytest.raises(store.VaultRefused, match="still being processed"):
        await store.seal(session, document, source, vault.id, user.id, key)
    assert blob_path(source.sha256).is_file()


async def test_it_refuses_when_the_original_is_already_missing(session, ready):
    user, _, document, source, vault, key = ready
    blob_path(source.sha256).unlink()

    with pytest.raises(store.VaultRefused, match="not on disk"):
        await store.seal(session, document, source, vault.id, user.id, key)


async def test_a_full_round_trip_restores_everything(session, ready):
    """In and back out: the bytes, the title and the pages."""
    user, _, document, source, vault, key = ready
    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()

    item = (
        await session.execute(
            sa.select(VaultItem).where(VaultItem.document_id == document.id)
        )
    ).scalar_one()
    await store.unseal(session, document, source, item, key)
    await session.commit()

    assert blob_path(source.sha256).read_bytes() == ORIGINAL
    assert document.title == "Deed — 14 Brackenridge Road"
    assert document.vaulted_by is None

    pages = (
        (
            await session.execute(
                sa.select(Page).where(Page.source_file_id == source.id).order_by(Page.page_number)
            )
        )
        .scalars()
        .all()
    )
    assert [p.page_number for p in pages] == [1, 2]
    assert PHRASE in pages[0].text


async def test_the_restored_original_is_read_only(session, ready):
    """Originals are immutable everywhere else (invariant 1), and one coming
    back out of the vault is not an exception."""
    import os

    user, _, document, source, vault, key = ready
    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()
    item = (
        await session.execute(
            sa.select(VaultItem).where(VaultItem.document_id == document.id)
        )
    ).scalar_one()
    await store.unseal(session, document, source, item, key)

    assert oct(os.stat(blob_path(source.sha256)).st_mode)[-3:] == "444"


async def test_one_documents_ciphertext_cannot_be_opened_as_another(session, ready):
    """The document id is authenticated alongside the bytes, so a vault object
    cannot be moved onto another row and still decrypt."""
    user, _, document, source, vault, key = ready
    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()
    item = (
        await session.execute(
            sa.select(VaultItem).where(VaultItem.document_id == document.id)
        )
    ).scalar_one()

    with pytest.raises(crypto.WrongSecret):
        store.open_object(item.object_name, uuid.uuid4(), key)


async def test_another_vaults_key_does_not_open_it(session, ready):
    user, _, document, source, vault, key = ready
    await store.seal(session, document, source, vault.id, user.id, key)
    await session.commit()
    item = (
        await session.execute(
            sa.select(VaultItem).where(VaultItem.document_id == document.id)
        )
    ).scalar_one()

    with pytest.raises(crypto.WrongSecret):
        store.open_object(item.object_name, document.id, crypto.new_data_key())
