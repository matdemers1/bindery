"""The vault has to know a photograph when it holds one (REQ-185).

The first version read `source.media_type`, and the field is `mime_type`. The
defensive `getattr(..., None)` around it swallowed the typo completely: every
vaulted item got a null type, every download was served as
`application/octet-stream`, and a photograph downloaded instead of displaying.

Nothing failed. That is the whole reason this file exists.
"""

import hashlib
import uuid

import pytest

from api.db.enums import IngestSource, ReviewState, SourceFileState
from api.db.models import Document, SourceFile
from api.storage.blobs import blob_path
from api.vault import service, store

BODY = b"\xff\xd8\xff\xe0not really a jpeg but it does not need to be"


@pytest.fixture
async def vaulted(session, signed_in, tmp_path, monkeypatch):
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    async def seal(filename: str, mime_type: str | None):
        user, library = await signed_in(
            email=f"u-{uuid.uuid4().hex[:8]}@example.test"
        )
        # The bytes and the digest must agree, or `seal` refuses the move —
        # as it should, and as it did when this fixture hashed one thing and
        # wrote another. Unique per call, too: each `signed_in()` makes a fresh
        # library, so a filename reused across tests put identical bytes in
        # several libraries at once, which `seal` now refuses because unlinking
        # a shared blob would destroy the other library's original.
        body = BODY + filename.encode() + uuid.uuid4().bytes
        digest = hashlib.sha256(body).hexdigest()
        path = blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

        source = SourceFile(
            library_id=library.id, sha256=digest, byte_size=len(body),
            original_filename=filename, mime_type=mime_type,
            ingest_source=IngestSource.WEB_UPLOAD, page_count=1,
            state=SourceFileState.PROCESSED,
        )
        session.add(source)
        await session.flush()
        document = Document(
            library_id=library.id, source_file_id=source.id,
            page_start=1, page_end=1, title="A private thing",
            review_state=ReviewState.FILED,
        )
        session.add(document)
        await session.flush()

        vault = await service.create(session, user.id, "a-long-enough-passphrase", "481516")
        key = service.require_key(user.id)
        await store.seal(session, document, source, vault.id, user.id, key)
        await session.commit()
        return user, document, key

    return seal


async def test_the_media_type_is_carried_into_the_vault(session, vaulted):
    """The bug, stated plainly: this was null for everything."""
    _user, document, key = await vaulted("holiday.jpg", "image/jpeg")

    import sqlalchemy as sa

    from api.db.models import VaultItem

    item = (
        await session.execute(
            sa.select(VaultItem).where(VaultItem.document_id == document.id)
        )
    ).scalar_one()
    assert item.original_media_type == "image/jpeg", (
        "the media type was dropped on the way in, so this downloads instead "
        "of displaying"
    )
    assert store.open_meta(item, key)["media_type"] == "image/jpeg"


async def test_a_null_type_falls_back_to_the_filename(session, vaulted):
    """Every item sealed before the fix has a null column. Guessing from the
    extension is weak in general and exactly right here — the alternative is
    octet-stream, under which a photograph is a file you save."""
    _user, document, key = await vaulted("holiday.jpg", None)

    import sqlalchemy as sa

    from api.db.models import VaultItem

    item = (
        await session.execute(
            sa.select(VaultItem).where(VaultItem.document_id == document.id)
        )
    ).scalar_one()
    assert item.original_media_type is None
    assert store.media_type_for(item, store.open_meta(item, key)) == "image/jpeg"


async def test_the_listing_says_which_items_are_pictures(client, session, vaulted):
    _user, document, _key = await vaulted("holiday.jpg", "image/jpeg")

    response = await client.get("/api/vault/items")
    assert response.status_code == 200, response.text
    row = next(r for r in response.json() if r["document_id"] == str(document.id))
    assert row["is_image"] is True
    assert row["media_type"] == "image/jpeg"


async def test_a_pdf_is_not_a_picture(client, session, vaulted):
    _user, document, _key = await vaulted("deed.pdf", "application/pdf")

    response = await client.get("/api/vault/items")
    row = next(r for r in response.json() if r["document_id"] == str(document.id))
    assert row["is_image"] is False
    assert row["media_type"] == "application/pdf"


async def test_the_original_is_served_as_an_image(client, session, vaulted):
    """What makes `<img src=…>` work at all."""
    _user, document, _key = await vaulted("holiday.jpg", "image/jpeg")

    response = await client.get(f"/api/vault/items/{document.id}/original")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["content-disposition"] == "inline"
    # Still never cached: a decrypted vault document in a browser cache
    # outlives the unlock that authorised it.
    assert "no-store" in response.headers["cache-control"]
    assert response.content.startswith(BODY + b"holiday.jpg")


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("photo.HEIC", True),
        ("scan.tiff", True),
        ("deed.pdf", False),
        ("notes.txt", False),
        (None, False),
    ],
)
def test_what_counts_as_a_picture(filename, expected):
    """HEIC included: an iPhone camera roll is most of what anyone vaults."""
    assert store.is_image(None, filename) is expected
