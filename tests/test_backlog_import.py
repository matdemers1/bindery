"""Phase 4 — backlog import (T-4.1 to T-4.7).

> Backlog import floods the review queue; triage becomes the new mess and the
> project is abandoned.

R-03, rated the most likely abandonment point in the plan. The tests that matter
most here are the ones that keep five thousand documents out of the daily queue
and make an interrupted import resumable rather than restartable.
"""

import hashlib
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.db.enums import ImportItemState, ImportState, IngestSource, SourceFileState
from api.db.models import (
    AuditEvent,
    Document,
    DocumentTag,
    ImportItem,
    ImportSession,
    SourceFile,
)
from worker.backlog.dryrun import estimate_cost
from worker.backlog.walker import walk


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A directory shaped like a real backlog: nested, noisy, partly junk.

    Contents are unique per test: the blob store is content-addressed, so
    identical fixtures would make one test's files look like duplicates of
    another's.
    """
    nonce = uuid.uuid4().hex.encode()
    (tmp_path / "2019").mkdir()
    (tmp_path / "2020" / "medical").mkdir(parents=True)
    (tmp_path / ".Trash").mkdir()
    (tmp_path / "node_modules").mkdir()

    (tmp_path / "2019" / "bill.pdf").write_bytes(b"%PDF-1.7\nbill" + nonce + b"\n%%EOF\n")
    (tmp_path / "2019" / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\nscan" + nonce)
    (tmp_path / "2020" / "medical" / "record.pdf").write_bytes(
        b"%PDF-1.7\nrecord" + nonce + b"\n%%EOF\n"
    )
    (tmp_path / "2020" / "notes.txt").write_text("not a document")
    (tmp_path / ".DS_Store").write_bytes(b"junk")
    (tmp_path / "2019" / "empty.pdf").write_bytes(b"")
    (tmp_path / ".Trash" / "deleted.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
    (tmp_path / "node_modules" / "vendor.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
    return tmp_path


@pytest.fixture
async def import_session(session, signed_in, tree):
    _, library = await signed_in()
    record = ImportSession(library_id=library.id, root_path=str(tree), sample_size=2)
    session.add(record)
    await session.commit()
    return library, record, tree


# --------------------------------------------------------------------------
# The walk (T-4.1)
# --------------------------------------------------------------------------


def test_the_walk_finds_documents_and_ignores_the_noise(tree) -> None:
    result = walk(tree)
    names = sorted(p.name for p in result.files)
    assert names == ["bill.pdf", "record.pdf", "scan.png"]
    assert result.skipped_unsupported == 1          # notes.txt
    assert result.skipped_hidden == 1               # .DS_Store
    assert any("empty file" in message for _, message in result.errors)


def test_the_walk_skips_directories_that_are_never_documents(tree) -> None:
    paths = {str(p) for p in walk(tree).files}
    assert not any(".Trash" in p or "node_modules" in p for p in paths)


def test_a_symlink_loop_does_not_hang_the_walk(tmp_path) -> None:
    """A backlog is twenty years of accretion; loops happen."""
    inner = tmp_path / "a" / "b"
    inner.mkdir(parents=True)
    (inner / "doc.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
    (inner / "loop").symlink_to(tmp_path / "a")

    result = walk(tmp_path, follow_symlinks=True)
    assert [p.name for p in result.files] == ["doc.pdf"]


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses directory permissions, so there is nothing to be denied",
)
def test_an_unreadable_directory_is_reported_not_fatal(tmp_path) -> None:
    """Failing on file 3,000 of 5,000 is how an import becomes one you never finish."""
    good = tmp_path / "good"
    good.mkdir()
    (good / "a.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "b.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
    locked.chmod(0o000)
    try:
        result = walk(tmp_path)
        assert [p.name for p in result.files] == ["a.pdf"]
    finally:
        locked.chmod(0o755)


# --------------------------------------------------------------------------
# The dry run (T-4.1, REQ-082)
# --------------------------------------------------------------------------


async def test_the_dry_run_reports_counts_without_ingesting(session, import_session) -> None:
    """Nothing is processed until a human has seen this."""
    from worker.backlog.session import scan

    _, record, _ = import_session
    await scan(session, record)
    await session.commit()

    assert record.state is ImportState.DRY_RUN
    assert record.dry_run["total_files"] == 3
    assert record.dry_run["by_extension"] == {".pdf": 2, ".png": 1}
    assert record.cost_estimate["batch_usd"] < record.cost_estimate["interactive_usd"]

    # Not one byte entered the archive from this scan.
    assert (
        await session.execute(
            sa.select(sa.func.count()).select_from(ImportItem).where(
                ImportItem.session_id == record.id,
                ImportItem.source_file_id.is_not(None),
            )
        )
    ).scalar_one() == 0


async def test_the_dry_run_notices_documents_already_in_the_archive(
    session, import_session
) -> None:
    from worker.backlog.session import scan

    library, record, tree = import_session
    payload = (tree / "2019" / "bill.pdf").read_bytes()
    session.add(SourceFile(
        library_id=library.id, sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload), ingest_source=IngestSource.WEB_UPLOAD,
    ))
    await session.commit()

    await scan(session, record)
    await session.commit()
    assert record.dry_run["already_in_archive"] == 1


def test_the_cost_estimate_halves_for_batch_and_flags_the_r06_alarm() -> None:
    small = estimate_cost(pages=100)
    assert small.batch_usd == pytest.approx(small.interactive_usd * 0.5, rel=0.01)
    assert small.exceeds_alarm is False

    # R-06's tripwire: over $150 projected means fall back to heuristics-only.
    huge = estimate_cost(pages=2_000_000)
    assert huge.exceeds_alarm is True


# --------------------------------------------------------------------------
# Resumability (T-4.5, REQ-086)
# --------------------------------------------------------------------------


async def test_rescanning_converges_rather_than_duplicating(session, import_session) -> None:
    from worker.backlog.session import scan

    _, record, _ = import_session
    await scan(session, record)
    await scan(session, record)
    await session.commit()

    count = (
        await session.execute(
            sa.select(sa.func.count()).select_from(ImportItem)
            .where(ImportItem.session_id == record.id)
        )
    ).scalar_one()
    assert count == 3


async def test_an_interrupted_import_resumes_where_it_stopped(session, import_session) -> None:
    from worker.backlog.session import ingest_batch, scan

    _, record, _ = import_session
    await scan(session, record)

    first = await ingest_batch(session, record, states=[ImportItemState.PENDING], limit=2)
    await session.commit()
    assert first == 2

    second = await ingest_batch(session, record, states=[ImportItemState.PENDING], limit=50)
    await session.commit()
    assert second == 1  # only the remaining one

    third = await ingest_batch(session, record, states=[ImportItemState.PENDING], limit=50)
    assert third == 0   # and nothing is reprocessed

    # Scoped to this session — source_file is shared across tests.
    files = (
        await session.execute(
            sa.select(sa.func.count()).select_from(ImportItem).where(
                ImportItem.session_id == record.id,
                ImportItem.source_file_id.is_not(None),
            )
        )
    ).scalar_one()
    assert files == 3


async def test_a_file_that_fails_does_not_stop_the_import(session, import_session) -> None:
    from worker.backlog.session import ingest_batch, scan

    _, record, tree = import_session
    await scan(session, record)
    (tree / "2019" / "bill.pdf").unlink()   # vanishes between scan and import

    done = await ingest_batch(session, record, states=[ImportItemState.PENDING], limit=50)
    await session.commit()

    assert done == 2
    failed = (
        await session.execute(
            sa.select(ImportItem).where(
                ImportItem.session_id == record.id,
                ImportItem.state == ImportItemState.FAILED.value,
            )
        )
    ).scalar_one()
    assert "disappeared" in failed.error


# --------------------------------------------------------------------------
# R-03: backlog stays out of the daily queue (T-4.3, REQ-083)
# --------------------------------------------------------------------------


async def test_imported_documents_are_flagged_and_excluded_from_review(
    client, session, import_session
) -> None:
    """**The R-03 mitigation.** The whole risk is triage becoming the new mess."""
    from worker.backlog.session import ingest_batch, mark_backlog, scan

    library, record, _ = import_session
    await scan(session, record)
    await ingest_batch(session, record, states=[ImportItemState.PENDING], limit=50)
    await session.flush()

    # Stand in for the pipeline, which would create these. Scoped to this
    # session's files: an unscoped select would pick up other tests' source
    # files, and the composite FK would (correctly) refuse the mismatch.
    files = (
        await session.execute(
            sa.select(SourceFile).where(
                SourceFile.id.in_(
                    sa.select(ImportItem.source_file_id).where(
                        ImportItem.session_id == record.id,
                        ImportItem.source_file_id.is_not(None),
                    )
                )
            )
        )
    ).scalars().all()
    for source_file in files:
        source_file.state = SourceFileState.PROCESSED
        session.add(Document(
            library_id=library.id, source_file_id=source_file.id,
            page_start=1, page_end=1, review_state="needs_review",
        ))
    await session.flush()

    flagged = await mark_backlog(session, record)
    await session.commit()
    assert flagged == 3

    # They need review, but they are not in the queue you open every morning.
    body = (await client.get("/api/review")).json()
    assert body["total"] == 0

    with_backlog = (await client.get("/api/review?include_backlog=true")).json()
    assert with_backlog["total"] == 3


# --------------------------------------------------------------------------
# Sampling and two passes (T-4.4, REQ-084)
# --------------------------------------------------------------------------


async def test_the_sample_is_spread_across_directories(session, import_session) -> None:
    """A backlog is organised by something; one folder is not representative."""
    from worker.backlog.session import scan, select_sample

    _, record, _ = import_session
    await scan(session, record)
    picked = await select_sample(session, record)
    await session.commit()

    assert picked >= 2
    sampled = (
        await session.execute(
            sa.select(ImportItem.path).where(
                ImportItem.session_id == record.id,
                ImportItem.state == ImportItemState.SAMPLED.value,
            )
        )
    ).scalars().all()
    directories = {str(Path(p).parent) for p in sampled}
    assert len(directories) >= 2


async def test_curation_sits_between_the_two_passes(client, session, import_session) -> None:
    from worker.backlog.session import scan

    _, record, _ = import_session
    await scan(session, record)
    await session.commit()

    body = (await client.post(f"/api/imports/{record.id}/curate")).json()
    assert body["state"] == "curating"
    assert body["pass_number"] == 2


# --------------------------------------------------------------------------
# Batch reconciliation (T-4.2, REQ-054)
# --------------------------------------------------------------------------


def test_custom_ids_are_stable_and_document_scoped() -> None:
    from worker.ai.batch import custom_id_for

    document_id = uuid.uuid4()
    assert custom_id_for(document_id) == custom_id_for(document_id)
    assert custom_id_for(document_id) != custom_id_for(uuid.uuid4())


# --------------------------------------------------------------------------
# Bulk edit (T-4.6, REQ-087, REQ-068)
# --------------------------------------------------------------------------


@pytest.fixture
async def many_documents(session, signed_in):
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        ingest_source=IngestSource.BULK_IMPORT, page_count=20,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    documents = [
        Document(library_id=library.id, source_file_id=source_file.id,
                 page_start=n, page_end=n, title=f"Doc {n}")
        for n in range(1, 21)
    ]
    session.add_all(documents)
    await session.commit()
    return library, documents


async def test_a_preview_writes_nothing(client, session, many_documents) -> None:
    _, documents = many_documents
    ids = [str(d.id) for d in documents]

    body = (await client.post("/api/bulk/preview", json={
        "document_ids": ids, "actions": {"add_tags": ["reviewed-2026"]}
    })).json()

    assert body["matched"] == 20
    assert body["operation_id"] is None
    assert (
        await session.execute(sa.select(sa.func.count()).select_from(DocumentTag))
    ).scalar_one() == 0


async def test_bulk_apply_then_undo_in_one_action(client, session, many_documents) -> None:
    """Undoing a thousand documents one at a time is the same as no undo."""
    _, documents = many_documents
    ids = [str(d.id) for d in documents]

    applied = (await client.post("/api/bulk/apply", json={
        "document_ids": ids,
        "actions": {"add_tags": ["reviewed-2026"], "set_sensitivity": "sensitive"},
    })).json()
    assert applied["matched"] == 20
    operation_id = applied["operation_id"]

    await session.commit()
    tagged = (
        await session.execute(
            sa.select(sa.func.count()).select_from(DocumentTag)
            .where(DocumentTag.removed_at.is_(None))
        )
    ).scalar_one()
    assert tagged == 20

    undone = (await client.post(f"/api/bulk/{operation_id}/undo")).json()
    assert undone["matched"] == 20

    await session.commit()
    still_tagged = (
        await session.execute(
            sa.select(sa.func.count()).select_from(DocumentTag)
            .where(DocumentTag.removed_at.is_(None))
        )
    ).scalar_one()
    assert still_tagged == 0

    for document in documents:
        await session.refresh(document)
        assert document.sensitivity.value == "normal"


async def test_a_bulk_operation_cannot_be_undone_twice(client, many_documents) -> None:
    _, documents = many_documents
    applied = (await client.post("/api/bulk/apply", json={
        "document_ids": [str(d.id) for d in documents[:3]],
        "actions": {"set_sensitivity": "vital"},
    })).json()
    operation_id = applied["operation_id"]

    assert (await client.post(f"/api/bulk/{operation_id}/undo")).status_code == 200
    assert (await client.post(f"/api/bulk/{operation_id}/undo")).status_code == 409


async def test_bulk_edit_records_one_event_with_a_manifest(
    client, session, many_documents
) -> None:
    _, documents = many_documents
    applied = (await client.post("/api/bulk/apply", json={
        "document_ids": [str(d.id) for d in documents],
        "actions": {"set_sensitivity": "vital"},
    })).json()

    # Scoped to this operation — the audit table is shared across tests.
    event = (
        await session.execute(
            sa.select(AuditEvent).where(AuditEvent.id == uuid.UUID(applied["operation_id"]))
        )
    ).scalar_one()
    assert event.action == "bulk_edit"
    assert len(event.before["manifest"]) == 20


async def test_bulk_edit_cannot_touch_another_library(
    client, session, many_documents, signed_in
) -> None:
    _, documents = many_documents
    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")

    body = (await client.post("/api/bulk/apply", json={
        "document_ids": [str(d.id) for d in documents], "actions": {"set_sensitivity": "vital"},
    })).json()
    assert body["matched"] == 0


async def test_an_unsupported_bulk_action_is_refused(client, many_documents) -> None:
    _, documents = many_documents
    response = await client.post("/api/bulk/apply", json={
        "document_ids": [str(documents[0].id)], "actions": {"delete_everything": True},
    })
    assert response.status_code == 422
    assert "unsupported bulk action" in response.json()["detail"]
