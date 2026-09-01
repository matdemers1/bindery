"""An import bound for the vault (T-18.10, T-18.11, REQ-196, REQ-197).

The worker cannot seal, so the api sweeps: a document from a vault-bound import
whose pipeline has finished is sealed the moment the owner's vault is open.
The two states that must stay distinguishable on screen are "done" and
"unlock to continue" — the second looks like a bug if it is not named.
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api.db.enums import (
    ImportItemState,
    ImportState,
    IngestSource,
    JobStage,
    JobState,
    ReviewState,
    SourceFileState,
)
from api.db.models import Document, ImportItem, ImportSession, Job, SourceFile
from api.storage.blobs import blob_path
from api.vault import service, sweep
from api.vault.session import sessions


@pytest.fixture
async def bound_import(session, signed_in, tmp_path, monkeypatch):
    """A vault-bound import with two files: one finished, one still in OCR."""
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    user, library = await signed_in()
    vault = await service.create(session, user.id, "a-long-enough-passphrase", "481516")

    import_session = ImportSession(
        library_id=library.id, root_path="/data/inbox", state=ImportState.IMPORTING,
        created_by=user.id, to_vault=True,
    )
    session.add(import_session)
    await session.flush()

    docs = {}
    for name, finished in (("done.jpg", True), ("still-in-ocr.jpg", False)):
        body = b"%JPG" + name.encode() + uuid.uuid4().bytes
        digest = hashlib.sha256(body).hexdigest()
        path = blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        source = SourceFile(
            library_id=library.id, sha256=digest, byte_size=len(body),
            original_filename=name, mime_type="image/jpeg",
            ingest_source=IngestSource.BULK_IMPORT, page_count=1,
            state=SourceFileState.PROCESSED if finished else SourceFileState.NORMALIZING,
        )
        session.add(source)
        await session.flush()
        document = Document(
            library_id=library.id, source_file_id=source.id, page_start=1, page_end=1,
            title=name, review_state=ReviewState.FILED,
        )
        session.add(document)
        session.add(
            ImportItem(
                session_id=import_session.id, path=f"/data/inbox/{name}",
                state=ImportItemState.INGESTED, source_file_id=source.id, sha256=digest,
            )
        )
        if not finished:
            session.add(
                Job(source_file_id=source.id, stage=JobStage.NORMALIZE, state=JobState.RUNNING)
            )
        await session.flush()
        docs[name] = document
    await session.commit()
    return user, vault, import_session, docs


async def test_a_finished_file_is_sealed_and_an_unfinished_one_waits(session, bound_import):
    """The whole mechanism in one pass."""
    user, _vault, _import_session, docs = bound_import
    assert sessions.is_unlocked(user.id)

    sealed = await sweep.sweep_once(session)

    assert sealed == 1
    await session.refresh(docs["done.jpg"])
    await session.refresh(docs["still-in-ocr.jpg"])
    assert docs["done.jpg"].vaulted_by == user.id
    assert docs["still-in-ocr.jpg"].vaulted_by is None, (
        "a file still being read was sealed — the pipeline now points at bytes "
        "that no longer exist"
    )


async def test_nothing_happens_while_the_vault_is_locked(session, bound_import):
    """Not an error and not a skip that loses the intent: the files wait, and
    the screen says so."""
    user, _vault, import_session, docs = bound_import
    sessions.lock(user.id)

    assert await sweep.sweep_once(session) == 0
    await session.refresh(docs["done.jpg"])
    assert docs["done.jpg"].vaulted_by is None
    assert await sweep.awaiting(session, import_session) == 1


async def test_the_counts_tell_done_from_waiting_apart(client, session, bound_import):
    _user, _vault, import_session, _docs = bound_import
    await sweep.sweep_once(session)

    response = await client.get(f"/api/imports/{import_session.id}")
    body = response.json()
    assert body["to_vault"] is True
    assert body["vaulted"] == 1
    assert body["awaiting_vault"] == 0, "the unfinished one is not *awaiting* — it is still in OCR"
    assert body["vault_unlocked"] is True


async def test_the_sweep_records_what_it_did(session, bound_import):
    from api.db.models import AuditEvent

    _user, _vault, _import_session, docs = bound_import
    await sweep.sweep_once(session)

    event = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == docs["done.jpg"].id, AuditEvent.action == "vault_in"
            )
        )
    ).scalar_one()
    assert "import_session" in event.after


async def test_a_second_pass_seals_nothing_twice(session, bound_import):
    await sweep.sweep_once(session)
    assert await sweep.sweep_once(session) == 0


async def test_an_import_bound_for_a_locked_vault_is_refused_up_front(
    client, session, signed_in, tmp_path
):
    """Refused now rather than accepted and never honoured."""
    user, library = await signed_in()
    await service.create(session, user.id, "a-long-enough-passphrase", "481516")
    await session.commit()
    sessions.lock(user.id)

    response = await client.post(
        "/api/imports",
        json={"library_id": str(library.id), "root_path": str(tmp_path), "to_vault": True},
    )
    assert response.status_code == 423
    assert "unlock" in response.json()["detail"]


async def test_the_inbox_preset_is_the_watched_folder(client, signed_in):
    await signed_in()
    response = await client.get("/api/imports/presets")
    assert response.status_code == 200, response.text
    assert response.json()["inbox"].startswith("/")


async def test_the_log_is_scoped_to_the_imports_files(client, session, bound_import):
    from api.db.models import EventLog

    _user, _vault, import_session, docs = bound_import
    mine = docs["done.jpg"].source_file_id
    session.add(EventLog(level="ERROR", logger="bindery.worker", message="OCR fell over",
                         source_file_id=mine, stage="normalize"))
    session.add(EventLog(level="ERROR", logger="bindery.worker", message="somebody else's file",
                         source_file_id=uuid.uuid4(), stage="normalize"))
    await session.commit()

    response = await client.get(f"/api/imports/{import_session.id}/log")
    assert response.status_code == 200, response.text
    messages = [row["message"] for row in response.json()]
    assert "OCR fell over" in messages
    assert "somebody else's file" not in messages
