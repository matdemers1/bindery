"""Upgrading v1 objects, and serving ranges from v2 (T-18.2, T-18.3, REQ-192).

Two properties must not regress, and both are here:

- **A vaulted byte is never served while locked**, range or not. The 423 comes
  before any chunk is touched.
- **Nothing is retired before its replacement has been read back and hashed.**
  The re-seal is the third path in the project that destroys something.
"""

import hashlib
import os

import pytest

from api.db.enums import IngestSource, ReviewState, SourceFileState
from api.db.models import Document, SourceFile, VaultItem
from api.vault import chunked, crypto, service, store
from api.vault.session import sessions

BODY = os.urandom(3 * chunked.CHUNK_SIZE + 4096)  # four chunks, last one short


@pytest.fixture
async def v1_item(session, signed_in, tmp_path, monkeypatch):
    """An item sealed the old way — one AES-GCM message — as the four in
    production were."""
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    user, library = await signed_in()
    digest = hashlib.sha256(BODY).hexdigest()
    source = SourceFile(
        library_id=library.id, sha256=digest, byte_size=len(BODY),
        original_filename="clip.mp4", mime_type="video/mp4",
        ingest_source=IngestSource.WEB_UPLOAD, page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    document = Document(
        library_id=library.id, source_file_id=source.id, page_start=1, page_end=1,
        review_state=ReviewState.FILED, vaulted_by=user.id,
    )
    session.add(document)
    await session.flush()

    vault = await service.create(session, user.id, "a-long-enough-passphrase", "481516")
    key = service.require_key(user.id)
    name = store.new_object_name()
    path = store.object_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        crypto.encrypt(
            BODY, crypto.file_key(key, name.encode()), associated=str(document.id).encode()
        )
    )
    item = VaultItem(
        document_id=document.id, vault_id=vault.id, object_name=name,
        byte_size=len(BODY), sealed_sha256=crypto.encrypt(digest.encode(), key),
        sealed_meta=crypto.encrypt(b'{"original_filename": "clip.mp4"}', key),
        original_media_type="video/mp4", page_count=1, format_version=1,
    )
    session.add(item)
    await session.commit()
    return user, document, item, key, digest


# --------------------------------------------------------------------------
# Re-seal (T-18.2)
# --------------------------------------------------------------------------


async def test_a_v1_object_is_upgraded_and_reads_back_identical(session, v1_item):
    _user, document, item, key, digest = v1_item
    old_path = store.object_path(item.object_name)

    assert await store.reseal(session, item, key) is True
    await session.commit()

    assert item.format_version == 2
    new_path = store.object_path(item.object_name)
    assert new_path != old_path
    assert store._is_v2(new_path)
    assert not old_path.exists(), "the old object was left behind"
    assert store.open_object(item.object_name, document.id, key) == BODY
    reader = chunked.Reader(new_path, crypto.file_key(key, item.object_name.encode()), document.id)
    assert reader.verify() == digest


async def test_reseal_is_a_no_op_on_a_v2_object(session, v1_item):
    _user, _document, item, key, _ = v1_item
    await store.reseal(session, item, key)
    name_after_first = item.object_name
    assert await store.reseal(session, item, key) is False
    assert item.object_name == name_after_first


async def test_reseal_refuses_an_object_that_does_not_match_its_hash(session, v1_item):
    """Not re-sealing something that is already wrong — and, above all, not
    deleting it."""
    _user, document, item, key, _ = v1_item
    # Re-encrypt different bytes under the same name: decrypts fine, hashes wrong.
    path = store.object_path(item.object_name)
    path.write_bytes(
        crypto.encrypt(
            b"not the original", crypto.file_key(key, item.object_name.encode()),
            associated=str(document.id).encode(),
        )
    )
    with pytest.raises(store.VaultRefused, match="already wrong"):
        await store.reseal(session, item, key)
    assert path.is_file(), "the old object was deleted despite the refusal"
    assert item.format_version == 1


async def test_the_reseal_verifies_before_it_retires() -> None:
    """Structural, like the seal's own guard: the read-back of the new object
    must precede the unlink of the old one in the source."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "api" / "vault" / "store.py").read_text()
    body = source[source.index("async def reseal("):]
    verify = body.index("chunked.Reader(new_path")
    retire = body.index("old_path.unlink")
    assert verify < retire, "the re-seal retires the old object before verifying the new one"
    assert "if not verified:" in body[:retire]


async def test_unlocking_upgrades_legacy_items(client, session, v1_item):
    """The re-seal runs on unlock, while the key is in hand — not on a timer
    that would need the key when nobody is there to provide it."""
    user, _document, item, _key, _ = v1_item
    sessions.lock(user.id)

    response = await client.post("/api/vault/unlock", json={"pin": "481516"})
    assert response.status_code == 200, response.text

    await session.refresh(item)
    assert item.format_version == 2


# --------------------------------------------------------------------------
# Ranges (T-18.3)
# --------------------------------------------------------------------------


async def _v2(session, v1_item):
    _user, document, item, key, _ = v1_item
    await store.reseal(session, item, key)
    await session.commit()
    return document, item


async def test_a_range_is_answered_with_206(client, session, v1_item):
    document, _ = await _v2(session, v1_item)
    start, end = chunked.CHUNK_SIZE - 10, chunked.CHUNK_SIZE + 10

    response = await client.get(
        f"/api/vault/items/{document.id}/original", headers={"Range": f"bytes={start}-{end}"}
    )
    assert response.status_code == 206, response.text
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(BODY)}"
    assert response.headers["accept-ranges"] == "bytes"
    assert "no-store" in response.headers["cache-control"]
    assert response.content == BODY[start : end + 1]


async def test_a_suffix_range_returns_the_tail(client, session, v1_item):
    """`bytes=-N` is how a player finds the moov atom in an .mp4."""
    document, _ = await _v2(session, v1_item)
    response = await client.get(
        f"/api/vault/items/{document.id}/original", headers={"Range": "bytes=-100"}
    )
    assert response.status_code == 206
    assert response.content == BODY[-100:]


async def test_an_open_ended_range_runs_to_the_end(client, session, v1_item):
    document, _ = await _v2(session, v1_item)
    start = 2 * chunked.CHUNK_SIZE + 7
    response = await client.get(
        f"/api/vault/items/{document.id}/original", headers={"Range": f"bytes={start}-"}
    )
    assert response.status_code == 206
    assert response.content == BODY[start:]


async def test_a_nonsense_range_gets_the_whole_file(client, session, v1_item):
    document, _ = await _v2(session, v1_item)
    response = await client.get(
        f"/api/vault/items/{document.id}/original", headers={"Range": "bytes=abc"}
    )
    assert response.status_code == 200
    assert response.content == BODY


async def test_no_range_is_the_whole_file(client, session, v1_item):
    document, _ = await _v2(session, v1_item)
    response = await client.get(f"/api/vault/items/{document.id}/original")
    assert response.status_code == 200
    assert response.content == BODY


async def test_a_range_never_reads_a_locked_vault(client, session, v1_item):
    """The property that must not regress. A range must never be a way to read
    one byte of a locked vault, and the refusal must come before any chunk is
    touched."""
    user, document, _item, _key, _ = v1_item
    sessions.lock(user.id)

    response = await client.get(
        f"/api/vault/items/{document.id}/original", headers={"Range": "bytes=0-10"}
    )
    assert response.status_code == 423
    assert BODY[:11] not in response.content


async def test_a_read_with_no_range_never_reads_a_locked_vault(client, session, v1_item):
    """The same property as the range case, on the path a download link takes.
    Streaming the body must not have moved the refusal after the first chunk."""
    user, document, _item, _key, _ = v1_item
    sessions.lock(user.id)

    response = await client.get(f"/api/vault/items/{document.id}/original")
    assert response.status_code == 423
    assert BODY[:11] not in response.content


async def test_the_whole_file_is_streamed_rather_than_joined(
    client, session, v1_item, monkeypatch
):
    """CR-033: the Range-less path used to `read_all` — every chunk decrypted
    and concatenated into one `bytes` before a byte was sent, which is the
    thing ADR-013 exists to prevent. It must never hold more than a chunk."""
    document, _ = await _v2(session, v1_item)

    def refuse(self):
        raise AssertionError("the whole plaintext was joined in memory")

    spans: list[int] = []
    read_range = chunked.Reader.read_range

    def record(self, start: int, end: int) -> bytes:
        spans.append(end - start + 1)
        return read_range(self, start, end)

    monkeypatch.setattr(chunked.Reader, "read_all", refuse)
    monkeypatch.setattr(chunked.Reader, "read_range", record)

    response = await client.get(f"/api/vault/items/{document.id}/original")
    assert response.status_code == 200
    assert response.content == BODY
    # The size is still declared, or a browser cannot scrub the video and a
    # download has no progress bar.
    assert response.headers["content-length"] == str(len(BODY))
    assert response.headers["accept-ranges"] == "bytes"
    assert "no-store" in response.headers["cache-control"]
    assert spans and max(spans) <= chunked.CHUNK_SIZE


async def test_decryption_does_not_run_on_the_event_loop(
    client, session, v1_item, monkeypatch
):
    """A 2 GB video decrypted inside the handler is 2 GB of one uvicorn loop
    doing nothing else (CR-033)."""
    import threading

    document, _ = await _v2(session, v1_item)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    read_range = chunked.Reader.read_range

    def record(self, start: int, end: int) -> bytes:
        seen.append(threading.get_ident())
        return read_range(self, start, end)

    monkeypatch.setattr(chunked.Reader, "read_range", record)

    assert (await client.get(f"/api/vault/items/{document.id}/original")).status_code == 200
    assert seen, "no chunk was decrypted"
    assert all(ident != loop_thread for ident in seen)
