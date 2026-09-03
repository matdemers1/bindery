"""The importer is a door, and it obeys the same limit the other one does.

`api/routers/upload.py` checks the account's storage quota before it stores a
byte. `api/backlog/session.ingest_batch` called `ingest.register` with no quota
check at all — so the way past the limit that exists to stop one household
filling the Zima's single disk pool was to point the importer at a folder
instead of dragging the files onto the page.

The failure mode is `api/quota.py`'s own: when the pool fills, OCR stops,
backups stop, and Postgres stops, for everyone. Content-addressed storage never
deletes, so the space is not recoverable through the application.

A refusal is per file and does not end the import (R-03): the item is marked
FAILED with the sentence that names the limit, and the walk goes on — the same
shape as every other thing that can go wrong with one file out of five thousand.
"""

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.backlog.session import charged_account, ingest_batch, scan
from api.db.enums import ImportItemState, IngestSource
from api.db.models import ImportItem, ImportSession, SourceFile


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    nonce = uuid.uuid4().hex.encode()
    for index in range(4):
        (tmp_path / f"statement-{index}.pdf").write_bytes(
            b"%PDF-1.7\n" + nonce + bytes([index]) * 400 + b"\n%%EOF\n"
        )
    return tmp_path


@pytest.fixture
async def scanned(session, signed_in, tree):
    user, library = await signed_in()
    record = ImportSession(
        library_id=library.id, root_path=str(tree), created_by=user.id
    )
    session.add(record)
    await session.commit()
    await scan(session, record)
    await session.commit()
    return user, library, record


async def _states(session, record) -> list[str]:
    return (
        await session.execute(
            sa.select(ImportItem.state).where(ImportItem.session_id == record.id)
        )
    ).scalars().all()


async def test_the_importer_stops_at_the_account_s_limit(session, scanned):
    """Room for some of them, and the rest are refused rather than stored."""
    user, library, record = scanned
    sizes = (
        await session.execute(
            sa.select(ImportItem.byte_size).where(ImportItem.session_id == record.id)
        )
    ).scalars().all()
    assert all(sizes), "the scan did not record sizes"
    # Enough for two of the four.
    user.storage_quota_bytes = sizes[0] + sizes[1]
    await session.commit()

    done = await ingest_batch(
        session, record, states=[ImportItemState.PENDING], limit=10
    )
    await session.commit()

    assert done == 2, f"{done} files were imported into room for two"
    states = await _states(session, record)
    assert states.count(ImportItemState.FAILED.value) == 2

    errors = (
        await session.execute(
            sa.select(ImportItem.error).where(
                ImportItem.session_id == record.id,
                ImportItem.state == ImportItemState.FAILED.value,
            )
        )
    ).scalars().all()
    assert all("storage limit" in (error or "") for error in errors), errors

    held = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(SourceFile.byte_size), 0)).where(
                SourceFile.library_id == library.id
            )
        )
    ).scalar_one()
    assert held <= user.storage_quota_bytes


async def test_an_unlimited_account_imports_everything(session, scanned):
    """The limit is the only thing added; nothing else about the import changed."""
    user, _library, record = scanned
    user.storage_quota_bytes = None
    await session.commit()

    done = await ingest_batch(
        session, record, states=[ImportItemState.PENDING], limit=10
    )
    await session.commit()
    assert done == 4
    assert set(await _states(session, record)) == {ImportItemState.INGESTED.value}


async def test_the_account_charged_is_the_one_that_started_the_import(session, scanned):
    """Whoever started it is the analogue of the uploader, and it is what
    `api/vault/sweep.py` already resolves an import by."""
    user, _library, record = scanned
    assert (await charged_account(session, record)).id == user.id


async def test_an_import_with_no_creator_falls_back_to_the_library_owner(
    session, scanned
):
    """Rows predate the `created_by` column. The bytes land in the owner's
    `usage_for` total either way, so the owner is who they are charged to."""
    user, _library, record = scanned
    record.created_by = None
    await session.commit()
    assert (await charged_account(session, record)).id == user.id


async def test_nothing_is_imported_when_there_is_nobody_to_charge(
    session, signed_in, tree
):
    """Fails closed, and says so on the item rather than 500ing the route."""
    _user, library = await signed_in()
    record = ImportSession(library_id=library.id, root_path=str(tree))
    session.add(record)
    await session.commit()
    await scan(session, record)
    await session.commit()
    await session.execute(
        sa.text("DELETE FROM membership WHERE library_id = :lid"),
        {"lid": library.id},
    )
    await session.commit()

    done = await ingest_batch(
        session, record, states=[ImportItemState.PENDING], limit=10
    )
    await session.commit()

    assert done == 0
    assert set(await _states(session, record)) == {ImportItemState.FAILED.value}
    assert (
        await session.execute(
            sa.select(sa.func.count()).select_from(SourceFile).where(
                SourceFile.library_id == library.id,
                SourceFile.ingest_source == IngestSource.BULK_IMPORT,
            )
        )
    ).scalar_one() == 0
