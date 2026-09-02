"""A vaulted original is sealed, not missing (Phase 16 follow-up).

Found on production, 47 minutes after the vault shipped and the user first
used it. Four photographs went into the vault, their plaintext was deleted —
which is the feature — and the integrity check reported four missing originals.

That is not a cosmetic false alarm. `run_backup` and `offsite.replicate` both
**refuse to run over a failing integrity check**, so the next scheduled
replication would have declined with "integrity check failed (4 missing)":
an archive that had stopped backing itself up, reporting data loss, because
nothing was lost.

The export and the restore drill were both taught about the vault in T-16.10
and T-16.12. This one was missed, and it sits upstream of both.
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api.db.enums import IngestSource, ReviewState, SourceFileState
from api.db.models import Document, SourceFile, VaultItem
from api.export import integrity
from api.storage.blobs import blob_path
from api.vault import crypto, store

BODY = b"%PDF-1.7\na photograph\n"


@pytest.fixture
async def sealed(session, signed_in, tmp_path, monkeypatch):
    """A document whose plaintext is gone and whose ciphertext is present."""
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    user, library = await signed_in()
    digest = hashlib.sha256(BODY).hexdigest()

    source = SourceFile(
        library_id=library.id, sha256=digest, byte_size=len(BODY),
        original_filename="photo.jpg", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    document = Document(
        library_id=library.id, source_file_id=source.id, page_start=1, page_end=1,
        review_state=ReviewState.FILED, vaulted_by=user.id,
    )
    session.add(document)
    await session.flush()

    from api.vault import service

    vault = await service.create(session, user.id, "a-long-enough-passphrase", "481516")
    key = service.require_key(user.id)
    object_name = store.new_object_name()
    path = store.object_path(object_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        crypto.encrypt(
            BODY,
            crypto.file_key(key, object_name.encode()),
            associated=str(document.id).encode(),
        )
    )
    session.add(
        VaultItem(
            document_id=document.id, vault_id=vault.id, object_name=object_name,
            byte_size=len(BODY), sealed_sha256=crypto.encrypt(digest.encode(), key),
            sealed_meta=crypto.encrypt(b"{}", key), page_count=1,
        )
    )
    await session.commit()
    # The plaintext really is gone — that is what `seal` does.
    blob_path(digest).unlink(missing_ok=True)
    return library, source, object_name


async def test_a_vaulted_original_is_not_reported_missing(session, sealed):
    """The bug, stated plainly."""
    library, source, _ = sealed
    report = await integrity.check(session, library_ids=[library.id])

    assert report.missing == [], (
        "a vaulted original was called missing — this stops the backups"
    )
    assert [row["sha256"] for row in report.sealed] == [source.sha256]


async def test_the_archive_stays_healthy(session, sealed):
    """`run_backup` and `replicate` both refuse over an unhealthy report, so
    this property is the difference between backing up and not."""
    library, _, _ = sealed
    report = await integrity.check(session, library_ids=[library.id])

    assert report.healthy is True
    assert report.ok == 1


async def test_a_lost_ciphertext_is_missing_and_says_so(session, sealed):
    """The check that matters more than the one it replaces. A blob can be
    re-scanned; a vault object exists once, and nothing else can produce it."""
    library, _source, object_name = sealed
    store.object_path(object_name).unlink()

    report = await integrity.check(session, library_ids=[library.id])

    assert report.healthy is False
    assert len(report.missing) == 1
    assert report.missing[0]["vault_object"] == object_name
    assert report.sealed == []


async def test_an_ordinary_archive_reports_nothing_sealed(
    session, signed_in, tmp_path, monkeypatch
):
    """Most archives have no vault, and a `sealed` list appearing out of
    nowhere would be its own false alarm."""
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    _, library = await signed_in()
    body = b"an ordinary document" + uuid.uuid4().bytes
    digest = hashlib.sha256(body).hexdigest()
    path = blob_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    source = SourceFile(
        library_id=library.id, sha256=digest, byte_size=len(body),
        original_filename="ordinary.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.commit()

    report = await integrity.check(session, library_ids=[library.id])

    assert report.sealed == []
    assert report.healthy is True
    assert report.ok == 1


async def test_a_file_half_in_the_vault_is_not_called_sealed(session, sealed):
    """The check that would have made the round-1 data-loss bug visible.

    "Sealed" is a property of the *file*, not of one document over it. Keying
    the sealed map on `source_file_id` meant a single vaulted document declared
    the whole file sealed — so the plaintext its siblings are still read from
    was never looked for, `report.healthy` stayed true, and `run_backup` and
    `replicate` both proceeded over a genuinely lost original (CR-024).

    `seal` now refuses a document whose file has live siblings, so this state
    should not be creatable going forward. It is still what an archive that
    already holds one looks like, and the check has to be right about it.
    """
    library, source, _ = sealed
    session.add(
        Document(
            library_id=library.id, source_file_id=source.id,
            page_start=2, page_end=2, review_state=ReviewState.FILED,
        )
    )
    await session.commit()

    report = await integrity.check(session, library_ids=[library.id])

    assert report.healthy is False, (
        "a file whose original is gone while a live document still reads it was "
        "reported healthy — this is what lets the backups run over data loss"
    )
    assert len(report.missing) == 1
    assert "partially vaulted" in report.missing[0]["reason"]
    assert report.sealed == [], "a half-vaulted file is not a sealed file"


async def test_a_superseded_sibling_does_not_make_a_sealed_file_look_half_vaulted(
    session, sealed
):
    """Segments are superseded, never deleted, so history sits on the same file.

    Counting those rows would report every re-segmented, fully vaulted file as
    partially vaulted — the false alarm this file exists to prevent, arriving
    from the other direction and stopping the backups just as effectively.
    """
    from datetime import UTC, datetime

    library, source, _ = sealed
    session.add(
        Document(
            library_id=library.id, source_file_id=source.id,
            page_start=1, page_end=1, review_state=ReviewState.FILED,
            superseded_at=datetime.now(UTC),
        )
    )
    await session.commit()

    report = await integrity.check(session, library_ids=[library.id])

    assert report.healthy is True
    assert len(report.sealed) == 1


async def test_every_vault_object_on_a_file_is_verified_not_just_one(session, sealed):
    """The dict comprehension kept the last `object_name` per file, so a file
    with two vaulted documents had exactly one ciphertext checked. The other
    could vanish and the report would still call the archive whole."""
    library, source, first_object = sealed

    owner_id = (
        await session.execute(
            sa.select(Document.vaulted_by).where(Document.vaulted_by.is_not(None)).limit(1)
        )
    ).scalar_one()
    vault_id = (
        await session.execute(sa.select(VaultItem.vault_id).limit(1))
    ).scalar_one()

    second = Document(
        library_id=library.id, source_file_id=source.id,
        page_start=2, page_end=2, review_state=ReviewState.FILED,
        vaulted_by=owner_id,
    )
    session.add(second)
    await session.flush()

    second_object = store.new_object_name()
    path = store.object_path(second_object)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"a second ciphertext")
    session.add(
        VaultItem(
            document_id=second.id, vault_id=vault_id, object_name=second_object,
            byte_size=19, sealed_sha256=b"ciphertext", sealed_meta=b"ciphertext",
            page_count=1,
        )
    )
    await session.commit()

    assert (await integrity.check(session, library_ids=[library.id])).healthy is True

    # Lose the object the old map would have discarded.
    store.object_path(first_object).unlink()
    report = await integrity.check(session, library_ids=[library.id])

    assert report.healthy is False
    assert [row["vault_object"] for row in report.missing] == [first_object]


async def test_hashing_the_archive_does_not_block_the_event_loop(
    session, signed_in, tmp_path, monkeypatch
):
    """`check` is `async def` and every byte of its work was synchronous.

    It runs from the worker's replication pass, over the whole archive, before
    every backup. Re-hashing hundreds of originals on the only event loop the
    worker has stalls the OCR slots and the health monitor for the length of the
    scan — and the health monitor is what would have reported the stall (CR-011).
    """
    import asyncio
    import time

    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    _, library = await signed_in()
    for index in range(3):
        body = b"an ordinary document " + uuid.uuid4().bytes
        digest = hashlib.sha256(body).hexdigest()
        path = blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        session.add(
            SourceFile(
                library_id=library.id, sha256=digest, byte_size=len(body),
                original_filename=f"ordinary-{index}.pdf",
                ingest_source=IngestSource.WEB_UPLOAD,
                page_count=1, state=SourceFileState.PROCESSED,
            )
        )
    await session.commit()

    real = integrity.hash_file

    def slow(path):
        time.sleep(0.05)
        return real(path)

    monkeypatch.setattr(integrity, "hash_file", slow)

    stalls: list[float] = []

    async def heartbeat():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            stalls.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    try:
        report = await integrity.check(session, library_ids=[library.id])
    finally:
        beat.cancel()

    assert report.ok == 3
    assert len(stalls) > 5, "the heartbeat never got to run at all"
    assert max(stalls) < 0.04, (
        f"the event loop was blocked for {max(stalls) * 1000:.0f}ms while the "
        "archive was re-hashed — the OCR slots and the health monitor with it"
    )
