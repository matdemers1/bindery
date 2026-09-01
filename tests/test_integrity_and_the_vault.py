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
