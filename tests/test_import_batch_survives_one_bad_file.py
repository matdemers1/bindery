"""One bad file must not end the import — including a bad file the *database*
rejects (REQ-086), and the scan that precedes it must not cost a round trip per
file.

`ingest_batch` already caught the exception per item. What it did not do was
recover the transaction: `register` inserts and flushes, so a database error
leaves the session in a failed transaction and every later statement in the
batch — including the FAILED marker that explains what happened — raises
`PendingRollbackError` out of the route.
"""

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from api import ingest
from api.backlog.session import ingest_batch, scan
from api.backlog.walker import walk
from api.db.enums import ImportItemState, ImportState
from api.db.models import ImportItem, ImportSession


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    nonce = uuid.uuid4().hex.encode()
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.pdf").write_bytes(
            b"%PDF-1.7\n" + name.encode() + nonce + b"\n%%EOF\n"
        )
    return tmp_path


@pytest.fixture
async def scanned(session, signed_in, tree):
    _, library = await signed_in()
    record = ImportSession(library_id=library.id, root_path=str(tree), sample_size=3)
    session.add(record)
    await session.commit()
    await scan(session, record)
    await session.commit()
    return library, record, tree


async def test_a_database_error_on_one_file_costs_only_that_file(
    session, scanned, monkeypatch
) -> None:
    """`b.pdf` trips a constraint; `a.pdf` and `c.pdf` still land.

    The failure is injected as a real statement error rather than a bare
    exception, because that is the difference: a Python error the handler
    catches is already survivable, and a *database* error poisons everything
    that follows it.
    """
    _library, record, _tree = scanned
    real_register = ingest.register

    async def register(db_session, blob, **kwargs):
        if kwargs.get("original_filename") == "b.pdf":
            await db_session.execute(sa.text("SELECT 1 / 0"))
        return await real_register(db_session, blob, **kwargs)

    monkeypatch.setattr(ingest, "register", register)

    done = await ingest_batch(
        session, record, states=[ImportItemState.PENDING], limit=50
    )
    await session.commit()

    assert done == 2, "the good files did not survive the bad one"
    states = {
        Path(item.path).name: item.state
        for item in (
            await session.execute(
                sa.select(ImportItem).where(ImportItem.session_id == record.id)
            )
        ).scalars().all()
    }
    assert states["a.pdf"] == ImportItemState.INGESTED
    assert states["c.pdf"] == ImportItemState.INGESTED
    # Invariant 8: the one that failed says so, on the screen, with a reason.
    assert states["b.pdf"] == ImportItemState.FAILED


async def test_the_failure_is_recorded_with_its_reason(
    session, scanned, monkeypatch
) -> None:
    _library, record, _tree = scanned

    async def register(db_session, blob, **kwargs):
        await db_session.execute(sa.text("SELECT 1 / 0"))

    monkeypatch.setattr(ingest, "register", register)

    assert await ingest_batch(
        session, record, states=[ImportItemState.PENDING], limit=50
    ) == 0
    await session.commit()

    items = (
        await session.execute(
            sa.select(ImportItem).where(ImportItem.session_id == record.id)
        )
    ).scalars().all()
    assert len(items) == 3
    assert all(item.state == ImportItemState.FAILED for item in items)
    assert all(item.error for item in items), "a failure with no reason is a silent one"


# --------------------------------------------------------------------------
# The scan (T-4.1, REQ-086)
# --------------------------------------------------------------------------


def test_the_walk_reports_the_size_it_already_read(tree) -> None:
    """It stat()s every file it finds; the importer should not stat them again."""
    result = walk(tree)
    assert set(result.sizes) == set(result.files)
    assert sum(result.sizes.values()) == result.total_bytes


async def test_the_scan_records_every_file_in_one_statement_per_batch(
    session, signed_in, tree, monkeypatch
) -> None:
    """20,000 files must not be 20,000 round trips inside one transaction."""
    from api.backlog import session as session_module

    monkeypatch.setattr(session_module, "INSERT_BATCH", 2)
    _, library = await signed_in()
    record = ImportSession(library_id=library.id, root_path=str(tree))
    session.add(record)
    await session.commit()

    from sqlalchemy import event

    from api.db.session import engine

    inserts = 0

    def count(conn, cursor, statement, parameters, context, executemany):
        nonlocal inserts
        if statement.lstrip().upper().startswith("INSERT INTO IMPORT_ITEM"):
            inserts += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        await scan(session, record)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    await session.commit()

    assert record.state == ImportState.DRY_RUN
    rows = (
        await session.execute(
            sa.select(ImportItem).where(ImportItem.session_id == record.id)
        )
    ).scalars().all()
    assert len(rows) == 3
    assert all(row.byte_size for row in rows), "the size the walk read was dropped"
    # Three files, two per statement: two inserts, not three.
    assert inserts == 2


async def test_rescanning_converges_rather_than_duplicating(session, scanned) -> None:
    _library, record, _tree = scanned
    await scan(session, record)
    await session.commit()

    assert (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(ImportItem)
            .where(ImportItem.session_id == record.id)
        )
    ).scalar_one() == 3
