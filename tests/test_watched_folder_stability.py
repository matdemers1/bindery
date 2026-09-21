"""The write-stability gate (REQ-003, BND-FR-008).

A scanner writes a multi-megabyte PDF over several seconds, and `inotify` — or
a poll — sees the file the moment it is created. Ingesting it then stores a
truncated document, content-addresses the truncation, and reports success: the
archive gains a file that looks fine in every listing and is missing its last
pages. Nothing downstream can tell.

The gate is four lines in `watch_inbox` — a file is only touched after two
consecutive scans observe the same size and mtime — and it had no test at all.
It is also the kind of code that reads as redundant to someone tidying up, which
is the combination worth defending.

Driven by running the real loop: the property is about what happens *between*
two scans, and a unit test of the comparison would assert the comparison rather
than the behaviour.
"""

import asyncio
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.db.models import SourceFile
from worker.ingest import watched_folder


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    """A writable DATA_ROOT, and a fast loop so a test is not five seconds a scan."""
    from api.config import get_settings

    root = tmp_path / "inbox"
    root.mkdir()
    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("INBOX_ROOT", str(root))
    get_settings.cache_clear()
    monkeypatch.setattr(watched_folder, "POLL_SECONDS", 0.01)
    yield root
    get_settings.cache_clear()


async def _scans(times: int, *, between=None) -> None:
    """Run the watcher for `times` passes, calling `between` after each one."""
    stopping = asyncio.Event()
    passes = {"count": 0}
    original = watched_folder._candidates

    def counted(path: Path):
        found = original(path)
        passes["count"] += 1
        if passes["count"] >= times:
            stopping.set()
        return found

    watched_folder._candidates = counted
    try:
        task = asyncio.create_task(watched_folder.watch_inbox(stopping))
        # One pass has to complete before the file changes, or the two
        # observations the gate compares are never taken.
        while passes["count"] < 1:
            await asyncio.sleep(0.005)
        if between is not None:
            between()
        await asyncio.wait_for(task, timeout=10)
    finally:
        watched_folder._candidates = original


async def _files(session, library_id) -> list[SourceFile]:
    return list(
        (
            await session.execute(
                sa.select(SourceFile).where(SourceFile.library_id == library_id)
            )
        )
        .scalars()
        .all()
    )


async def test_a_file_still_being_written_is_not_ingested(
    session, signed_in, inbox
) -> None:
    """The failure this exists to prevent: a truncated scan, stored as whole.

    A content address over half a file is a perfectly valid content address, so
    nothing downstream — not the integrity check, not the restore drill — can
    tell it apart from a document that was always that length.
    """
    _, library = await signed_in()
    directory = inbox / watched_folder._slug(library.name)
    directory.mkdir()
    growing = directory / "scan.pdf"
    growing.write_bytes(b"%PDF-1.4 first half")

    # The file changes between the two scans, exactly as a scanner's write does.
    await _scans(2, between=lambda: growing.write_bytes(b"%PDF-1.4 first half and the rest"))

    assert growing.exists(), "it must be left where it is, not retired"
    assert await _files(session, library.id) == []


async def test_a_file_that_has_stopped_changing_is_ingested(
    session, signed_in, inbox
) -> None:
    """And the gate is a delay, not a refusal — the next scan picks it up."""
    _, library = await signed_in()
    directory = inbox / watched_folder._slug(library.name)
    directory.mkdir()
    settled = directory / f"{uuid.uuid4().hex[:8]}.pdf"
    settled.write_bytes(b"%PDF-1.4 complete")

    await _scans(2)

    files = await _files(session, library.id)
    assert len(files) == 1, "two identical observations is the whole condition"
    assert files[0].original_filename == settled.name
    assert not settled.exists(), "and it is retired out of the inbox once taken"


async def test_an_empty_file_is_never_ingested_however_stable(
    session, signed_in, inbox
) -> None:
    """Zero bytes is stable forever, so the gate alone would pass it through.

    It is retired to `.failed` rather than left in place: a file that will never
    be ingested and never moves is one the watcher re-examines on every scan for
    the life of the archive, and which nothing tells anyone about.
    """
    _, library = await signed_in()
    directory = inbox / watched_folder._slug(library.name)
    directory.mkdir()
    empty = directory / "nothing.pdf"
    empty.touch()

    await _scans(2)

    assert await _files(session, library.id) == []
    assert not empty.exists()
    assert (directory / watched_folder.FAILED_DIR / "nothing.pdf").exists()
