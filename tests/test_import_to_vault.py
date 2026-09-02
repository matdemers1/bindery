"""An import bound for the vault (T-18.10, T-18.11, REQ-196, REQ-197).

The worker cannot seal, so the api sweeps: a document from a vault-bound import
whose pipeline has finished is sealed the moment the owner's vault is open.
The two states that must stay distinguishable on screen are "done" and
"unlock to continue" — the second looks like a bug if it is not named.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

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
from api.vault.session import IDLE_TIMEOUT, sessions


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


async def test_a_queued_classification_elsewhere_does_not_stop_the_sweep(
    session, bound_import
):
    """The "is this file still being read?" check has to survive a NULL.

    Classify and rules jobs carry a document and no source file, so the subquery
    that lists busy files contains NULLs — and `x NOT IN (…, NULL)` is never
    true. One queued classification anywhere in the archive, which is the normal
    state during any import, emptied the ready list entirely: the sweep sealed
    nothing, said nothing, and every file sat on the screen as "awaiting".
    """
    user, _vault, _import_session, docs = bound_import
    user_id = user.id
    session.add(
        Job(
            document_id=docs["still-in-ocr.jpg"].id,
            stage=JobStage.CLASSIFY,
            state=JobState.QUEUED,
        )
    )
    await session.commit()

    assert await sweep.sweep_once(session) >= 1
    await session.refresh(docs["done.jpg"])
    assert docs["done.jpg"].vaulted_by == user_id


async def test_a_sweep_tick_is_not_activity(session, bound_import):
    """ADR-012's idle timeout has to survive the sweep that reads it.

    The sweep asks every 15 seconds whether each vault-bound import's owner is
    unlocked. When that question extended the idle window, an account with one
    such import — the normal state after any vault import, since nothing clears
    the flag — could never idle out, and the vault stayed open until the api
    process restarted. The person's screen said "unlocked" and gave them no way
    to find out why.
    """
    user, _vault, _import_session, _docs = bound_import
    user_id = user.id

    # Last used by a person fourteen minutes ago: still open, one minute left.
    held = sessions._by_user[user_id]
    held.touched_at = datetime.now(UTC) - timedelta(minutes=14)
    idle_since = held.touched_at

    await sweep.sweep_once(session)

    assert sessions._by_user[user_id].touched_at == idle_since, (
        "the sweep counted its own poll as use, so this vault will never close"
    )

    # And the window actually closes with ticks still running.
    sessions._by_user[user_id].touched_at = (
        datetime.now(UTC) - IDLE_TIMEOUT - timedelta(minutes=1)
    )
    # Not asserted on the return: the sweep is global and other tests in this
    # module leave bound, unlocked sessions behind. The claim is about *this*
    # account's key.
    await sweep.sweep_once(session)
    assert not sessions.is_unlocked(user_id), "an idle vault stayed open across a sweep"


async def test_an_import_bound_for_a_locked_vault_is_refused_up_front(
    client, session, signed_in
):
    """Refused now rather than accepted and never honoured."""
    from api.config import get_settings

    user, library = await signed_in()
    await service.create(session, user.id, "a-long-enough-passphrase", "481516")
    await session.commit()
    sessions.lock(user.id)

    response = await client.post(
        "/api/imports",
        # An importable root, so the refusal under test is the locked vault and
        # not the containment check an arbitrary path now meets first.
        json={
            "library_id": str(library.id),
            "root_path": str(get_settings().inbox_root),
            "to_vault": True,
        },
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


# --------------------------------------------------------------------------
# The flag belongs to the import, not the scan
# --------------------------------------------------------------------------


async def test_a_completed_import_can_be_bound_afterwards(client, session, bound_import):
    """The bug, stated plainly: 309 files went into the ordinary archive with
    the box ticked, because the flag was read at scan time and the person
    ticked it after scanning — the natural order. Binding afterwards is also
    how those files get where they were meant to go."""
    user, _vault, import_session, docs = bound_import
    # Plain value now: the sweep may roll the shared session back, which
    # expires `user`, and a sync attribute access on it afterwards fails.
    user_id = user.id
    import_session.to_vault = False
    import_session.state = ImportState.COMPLETED
    await session.commit()

    response = await client.patch(
        f"/api/imports/{import_session.id}", json={"to_vault": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["to_vault"] is True
    # The finished one is now waiting for the sweep; the next tick seals it.
    assert response.json()["awaiting_vault"] == 1

    # `>= 1`, not `== 1`: the sweep is global by design and other tests in
    # this module leave bound, unlocked sessions behind. The claim here is
    # about *this* document.
    assert await sweep.sweep_once(session) >= 1
    await session.refresh(docs["done.jpg"])
    assert docs["done.jpg"].vaulted_by == user_id


async def test_binding_is_refused_while_the_vault_is_locked(client, session, bound_import):
    user, _vault, import_session, _docs = bound_import
    import_session.to_vault = False
    await session.commit()
    sessions.lock(user.id)

    response = await client.patch(
        f"/api/imports/{import_session.id}", json={"to_vault": True}
    )
    assert response.status_code == 423
    await session.refresh(import_session)
    assert import_session.to_vault is False


async def test_unbinding_needs_no_unlock(client, session, bound_import):
    """Taking the vault out of the plan is never something the vault has to be
    open for."""
    user, _vault, import_session, _docs = bound_import
    sessions.lock(user.id)
    response = await client.patch(
        f"/api/imports/{import_session.id}", json={"to_vault": False}
    )
    assert response.status_code == 200
    assert response.json()["to_vault"] is False


async def test_the_run_call_carries_the_choice_made_at_that_moment(
    client, session, bound_import
):
    _user, _vault, import_session, _docs = bound_import
    import_session.to_vault = False
    await session.commit()

    response = await client.post(f"/api/imports/{import_session.id}/run?to_vault=true")
    assert response.status_code == 200, response.text
    await session.refresh(import_session)
    assert import_session.to_vault is True


async def test_binding_is_audited(client, session, bound_import):
    from api.db.models import AuditEvent

    _user, _vault, import_session, _docs = bound_import
    import_session.to_vault = False
    await session.commit()
    await client.patch(f"/api/imports/{import_session.id}", json={"to_vault": True})

    event = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == import_session.id,
                AuditEvent.action == "import_to_vault",
            )
        )
    ).scalars().first()
    assert event is not None
    assert event.before == {"to_vault": False}



async def test_one_refused_seal_does_not_stop_the_rest_of_the_tick(session, bound_import):
    """The first version rolled back after a refusal and then touched the next
    document's expired attributes, so one file with a missing blob broke the
    whole tick — every tick — and nothing else in the import ever sealed."""
    from api.db.models import ImportItem

    user, _vault, import_session, docs = bound_import
    user_id = user.id  # see test_a_completed_import_can_be_bound_afterwards
    body = b"gone" + uuid.uuid4().bytes
    digest = hashlib.sha256(body).hexdigest()
    source = SourceFile(
        library_id=docs["done.jpg"].library_id, sha256=digest, byte_size=len(body),
        original_filename="gone.jpg", mime_type="image/jpeg",
        ingest_source=IngestSource.BULK_IMPORT, page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    gone = Document(
        library_id=source.library_id, source_file_id=source.id, page_start=1, page_end=1,
        title="gone.jpg", review_state=ReviewState.FILED,
    )
    session.add(gone)
    session.add(
        ImportItem(
            session_id=import_session.id, path="/data/inbox/gone.jpg",
            state=ImportItemState.INGESTED, source_file_id=source.id, sha256=digest,
        )
    )
    await session.commit()

    sealed = await sweep.sweep_once(session)

    assert sealed >= 1, "the refusal took the whole tick down"
    await session.refresh(docs["done.jpg"])
    await session.refresh(gone)
    assert docs["done.jpg"].vaulted_by == user_id, "the good file was not sealed"
    assert gone.vaulted_by is None, "a file with no original must not be marked vaulted"
