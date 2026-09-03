"""What the sweep may seal, and what a tick costs when there is nothing to do.

Two properties of the same loop (ADR-012, REQ-197). It seals the files an import
*brought in* — not the ones it recognised as already filed — and a tick with no
vault open is one narrow query, not a query per import anybody has ever bound.
"""

import hashlib
import uuid

import pytest
from sqlalchemy import event

from api.db.enums import (
    ImportItemState,
    ImportState,
    IngestSource,
    ReviewState,
    SourceFileState,
)
from api.db.models import Document, ImportItem, ImportSession, SourceFile
from api.db.session import engine
from api.storage.blobs import blob_path
from api.vault import service, sweep
from api.vault.session import sessions


@pytest.fixture
async def vault_bound_import(session, signed_in, tmp_path, monkeypatch):
    """A vault-bound import of one new file, in a fresh data root."""
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    # `sessions` is process-global and the database is shared, so an owner left
    # unlocked by an earlier module still has a vault-bound import here — with
    # its blobs under a DATA_ROOT that no longer exists. The sweep would reach
    # that owner first, refuse, and roll back. Start from one open vault: this
    # test is about which files a sweep may seal, not about whose.
    sessions.lock_everything()

    user, library = await signed_in()
    await service.create(session, user.id, "a-long-enough-passphrase", "481516")
    import_session = ImportSession(
        library_id=library.id, root_path="/data/inbox", state=ImportState.IMPORTING,
        created_by=user.id, to_vault=True,
    )
    session.add(import_session)
    await session.flush()
    return user, library, import_session


async def _finished_file(session, library, name: str) -> tuple[SourceFile, Document]:
    body = b"%JPG" + name.encode() + uuid.uuid4().bytes
    digest = hashlib.sha256(body).hexdigest()
    path = blob_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    source = SourceFile(
        library_id=library.id, sha256=digest, byte_size=len(body),
        original_filename=name, mime_type="image/jpeg",
        ingest_source=IngestSource.BULK_IMPORT, page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    document = Document(
        library_id=library.id, source_file_id=source.id, page_start=1, page_end=1,
        title=name, review_state=ReviewState.FILED,
    )
    session.add(document)
    await session.flush()
    return source, document


async def test_a_duplicate_does_not_drag_the_archive_copy_into_the_vault(
    session, vault_bound_import
) -> None:
    """Re-importing a statement you already filed must not vault the old one.

    `ingest.register` recognises the bytes and marks the item DUPLICATE against
    the source file that was *already* in the archive. The sweep joined
    `import_item` with no state filter, so that pre-existing file's documents
    counted as this import's work: title blanked into `sealed_meta`, taxonomy
    and known-form links cleared, tags revoked, plaintext destroyed, gone from
    the archive — none of it asked for by the person who ticked "straight into
    the vault" on a folder of new files.
    """
    user, library, import_session = vault_bound_import
    brought_in, new_document = await _finished_file(session, library, "new.jpg")
    already_here, old_document = await _finished_file(session, library, "filed-in-2019.jpg")
    session.add_all(
        [
            ImportItem(
                session_id=import_session.id, path="/data/inbox/new.jpg",
                state=ImportItemState.INGESTED, source_file_id=brought_in.id,
                sha256=brought_in.sha256,
            ),
            ImportItem(
                session_id=import_session.id, path="/data/inbox/filed-in-2019.jpg",
                state=ImportItemState.DUPLICATE, source_file_id=already_here.id,
                sha256=already_here.sha256,
            ),
        ]
    )
    await session.commit()

    assert await sweep.awaiting(session, import_session) == 1, (
        "the duplicate's archive copy was counted as this import's work"
    )
    await sweep.sweep_once(session)

    await session.refresh(new_document)
    await session.refresh(old_document)
    assert new_document.vaulted_by == user.id
    assert old_document.vaulted_by is None, "a document nobody imported was sealed"
    assert old_document.title == "filed-in-2019.jpg"


async def test_a_tick_with_no_vault_open_is_one_query(session, vault_bound_import) -> None:
    """The cost of doing nothing has to stay flat.

    Every vault-bound import that has ever existed was asked about on every
    tick — a vault lookup plus a three-way join with a NOT IN subquery, four
    times a minute, for imports that finished months ago.
    """
    _user, library, import_session = vault_bound_import
    source, _document = await _finished_file(session, library, "waiting.jpg")
    session.add(
        ImportItem(
            session_id=import_session.id, path="/data/inbox/waiting.jpg",
            state=ImportItemState.INGESTED, source_file_id=source.id, sha256=source.sha256,
        )
    )
    await session.commit()
    # Nobody is here. Locking everything rather than this one account because
    # the sweep is global, and what is being measured is a tick, not an account.
    sessions.lock_everything()

    statements = 0

    def count(conn, cursor, statement, parameters, context, executemany):
        nonlocal statements
        statements += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        assert await sweep.sweep_once(session) == 0
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)

    assert statements == 1, (
        f"a tick with no vault open cost {statements} statements; it should ask "
        "once whether anybody is unlocked and stop"
    )


async def test_the_tick_still_expires_an_idle_vault(session, vault_bound_import) -> None:
    """The short-circuit must not skip the read that drops an expired key.

    `peek` is where an idle vault is noticed and relocked. Asking it only for
    owners the sweep still has work for would leave a vault open, on screen,
    long past ADR-012's fifteen minutes.
    """
    from datetime import UTC, datetime, timedelta

    from api.vault.session import IDLE_TIMEOUT

    user, _library, _import_session = vault_bound_import
    await session.commit()
    sessions._by_user[user.id].touched_at = (
        datetime.now(UTC) - IDLE_TIMEOUT - timedelta(minutes=1)
    )

    await sweep.sweep_once(session)

    assert user.id not in sessions._by_user, "an idle vault stayed open across a tick"


async def test_a_sweep_tick_is_still_not_activity(session, vault_bound_import) -> None:
    from datetime import UTC, datetime, timedelta

    user, _library, _import_session = vault_bound_import
    await session.commit()
    held = sessions._by_user[user.id]
    held.touched_at = datetime.now(UTC) - timedelta(minutes=14)
    idle_since = held.touched_at

    await sweep.sweep_once(session)

    assert sessions._by_user[user.id].touched_at == idle_since, (
        "the sweep counted its own poll as use, so this vault will never close"
    )


@pytest.fixture(autouse=True)
def _leave_no_vault_open():
    yield
    sessions.lock_everything()


def test_the_ready_query_asks_for_files_this_import_brought_in() -> None:
    """Structural, like the guards in `tests/test_no_destructive_paths.py`.

    The state filter is one line and its absence is invisible: every test with
    an INGESTED fixture passes either way, and the only file that behaves
    differently is one somebody already had.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "api/vault/sweep.py").read_text()
    assert "ImportItemState.INGESTED" in source, (
        "the sweep may seal only the files an import actually brought in"
    )
