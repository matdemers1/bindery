"""Persisted diagnostics (T-8.11).

The failure being fixed: every log line the application wrote went to the
container's stdout and nowhere else, so "why did this file not work" meant SSH
to the host and hope the line had not scrolled away. A truncated `last_error`
on the job, overwritten by the next attempt, was the only durable trace.

The tests that matter here are the ones about *not* breaking things. A logging
handler that can raise takes down the code it was watching, and a log row that
rolls back with the transaction it was describing is worse than no row at all.
"""

import logging
import uuid

import pytest
import sqlalchemy as sa

from api import eventlog
from api.db.enums import IngestSource, SourceFileState
from api.db.models import EventLog, SourceFile
from api.db.session import SessionFactory


@pytest.fixture(autouse=True)
def clean_queue():
    """Each test owns the queue: it is process-global."""
    while True:
        try:
            eventlog._records.get_nowait()
        except Exception:
            break
    eventlog._dropped = 0
    yield


@pytest.fixture
def handler():
    installed = eventlog.install()
    logging.getLogger("bindery.test").setLevel(logging.INFO)
    yield installed
    logging.getLogger().removeHandler(installed)


# --------------------------------------------------------------------------
# It must not break what it watches
# --------------------------------------------------------------------------


def test_emit_never_raises_even_on_a_hostile_record(handler) -> None:
    """A handler that raises takes down the code it was watching."""

    class Exploding:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    record = logging.LogRecord(
        "bindery.test", logging.INFO, __file__, 1, "value=%s", (Exploding(),), None
    )
    handler.emit(record)  # must not raise
    assert eventlog.dropped() == 1, "and the loss is counted rather than silent"


def test_a_full_queue_drops_rather_than_blocks(handler) -> None:
    """Blocking here would stall a pipeline stage on a log line."""
    for index in range(eventlog.QUEUE_SIZE + 25):
        handler.emit(
            logging.LogRecord("bindery.test", logging.INFO, __file__, 1, f"line {index}", (), None)
        )

    assert eventlog.pending() == eventlog.QUEUE_SIZE
    assert eventlog.dropped() >= 25


async def test_a_drain_failure_does_not_propagate(handler) -> None:
    """The archive keeps working when the log table does not."""
    handler.emit(
        logging.LogRecord("bindery.test", logging.INFO, __file__, 1, "hello", (), None)
    )

    def broken_factory():
        raise RuntimeError("database is on fire")

    assert await eventlog.drain_once(broken_factory) == 0


async def test_dropped_records_are_reported_through_the_log_itself(handler) -> None:
    eventlog._dropped = 7
    handler.emit(
        logging.LogRecord("bindery.test", logging.INFO, __file__, 1, "hello", (), None)
    )
    await eventlog.drain_once(SessionFactory)

    async with SessionFactory() as session:
        note = (
            await session.execute(
                sa.select(EventLog).where(EventLog.message.like("%were dropped%"))
            )
        ).scalars().first()
    assert note is not None
    assert "7 log record(s)" in note.message


# --------------------------------------------------------------------------
# What it captures
# --------------------------------------------------------------------------


async def test_existing_log_calls_are_captured_without_being_rewritten(handler) -> None:
    """The whole reason this is a handler: 75 log statements already said the
    right things to the wrong place, and none of them had to change."""
    logging.getLogger("bindery.test").warning("normalized %s", "scan.pdf")
    await eventlog.drain_once(SessionFactory)

    async with SessionFactory() as session:
        row = (
            await session.execute(
                sa.select(EventLog).where(EventLog.message == "normalized scan.pdf")
            )
        ).scalars().one()
    assert row.level == "warning"
    assert row.logger == "bindery.test"


def test_other_libraries_logging_is_not_swallowed_into_the_archive(handler) -> None:
    """SQLAlchemy and uvicorn at INFO would bury the pipeline's own story."""
    logging.getLogger("sqlalchemy.engine").warning("some pool detail")
    logging.getLogger("bindery.test").warning("ours")
    assert eventlog.pending() == 1


def test_debug_is_not_persisted(handler) -> None:
    logging.getLogger("bindery.test").debug("noisy")
    assert eventlog.pending() == 0


async def test_a_traceback_is_kept_whole(handler) -> None:
    """The bottom of a traceback is the useful end, so it is not truncated."""
    try:
        raise ValueError("the actual cause")
    except ValueError:
        logging.getLogger("bindery.test").exception("stage failed")
    await eventlog.drain_once(SessionFactory)

    async with SessionFactory() as session:
        row = (
            await session.execute(
                sa.select(EventLog).where(EventLog.message == "stage failed")
            )
        ).scalars().one()
    assert row.detail and "the actual cause" in row.detail
    assert "ValueError" in row.detail


async def test_context_travels_with_the_work(handler) -> None:
    """A stage binds the file once; everything logged inside is attributable —
    including lines from code that has never heard of the event log."""
    file_id = uuid.uuid4()
    library_id = uuid.uuid4()

    with eventlog.bind(source_file_id=file_id, library_id=library_id, stage="normalize"):
        logging.getLogger("bindery.somewhere.deep").info("a line from library code")

    logging.getLogger("bindery.test").info("outside the block")
    await eventlog.drain_once(SessionFactory)

    async with SessionFactory() as session:
        inside = (
            await session.execute(
                sa.select(EventLog).where(EventLog.message == "a line from library code")
            )
        ).scalars().one()
        outside = (
            await session.execute(
                sa.select(EventLog).where(EventLog.message == "outside the block")
            )
        ).scalars().one()

    assert inside.source_file_id == file_id
    assert inside.stage == "normalize"
    assert outside.source_file_id is None, "the binding must not leak past its block"


def test_nested_binds_merge(handler) -> None:
    outer = uuid.uuid4()
    with eventlog.bind(source_file_id=outer):
        with eventlog.bind(stage="page", page=3):
            assert eventlog.context()["source_file_id"] == outer
            assert eventlog.context()["page"] == 3
        assert "stage" not in eventlog.context()


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


@pytest.fixture
async def logged_file(session, signed_in, handler):
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="mine.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.commit()

    with eventlog.bind(library_id=library.id, source_file_id=source_file.id):
        logging.getLogger("bindery.test").error("could not read mine.pdf")
    await eventlog.drain_once(SessionFactory)
    return library, source_file


async def test_logs_are_readable_by_their_own_library(client, logged_file) -> None:
    response = await client.get("/api/logs")
    assert response.status_code == 200, response.text
    assert "could not read mine.pdf" in response.text


async def test_logs_do_not_leak_across_libraries(client, logged_file, signed_in) -> None:
    """Log messages routinely contain filenames, so the boundary that governs a
    document has to govern its diagnostics."""
    await signed_in()  # now a different user, in a different library
    response = await client.get("/api/logs")
    assert response.status_code == 200, response.text
    assert "mine.pdf" not in response.text


async def test_filtering_by_level_is_a_minimum_not_an_exact_match(
    client, logged_file, handler
) -> None:
    """Asking for warnings and being shown no errors is the opposite of useful."""
    library, _ = logged_file
    with eventlog.bind(library_id=library.id):
        logging.getLogger("bindery.test").warning("a mere warning")
    await eventlog.drain_once(SessionFactory)

    body = (await client.get("/api/logs", params={"level": "warning"})).json()
    messages = [entry["message"] for entry in body["entries"]]
    assert "a mere warning" in messages
    assert "could not read mine.pdf" in messages, "errors outrank warnings"


async def test_filtering_to_one_file(client, logged_file) -> None:
    _, source_file = logged_file
    body = (
        await client.get("/api/logs", params={"source_file_id": str(source_file.id)})
    ).json()
    assert body["entries"]
    assert all(e["source_file_id"] == str(source_file.id) for e in body["entries"])


# --------------------------------------------------------------------------
# Per-file pipeline progress
# --------------------------------------------------------------------------


async def test_progress_reports_where_each_file_is(client, session, signed_in) -> None:
    from api.db.enums import JobStage, JobState
    from api.db.models import Job

    _, library = await signed_in()
    moving = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="moving.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.PAGING,
    )
    broken = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="broken.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.RECEIVED,
    )
    session.add_all([moving, broken])
    await session.flush()
    session.add(
        Job(
            source_file_id=broken.id, stage=JobStage.NORMALIZE,
            state=JobState.DEAD_LETTER, attempts=5,
            last_error="CommandError('ocrmypdf exited 2: InputFileError')",
        )
    )
    await session.commit()

    body = (await client.get("/api/pipeline/files")).json()
    files = {f["original_filename"]: f for f in body["files"]}

    assert files["moving.pdf"]["state"] == "paging"
    assert files["moving.pdf"]["dead_lettered"] is False
    assert files["broken.pdf"]["dead_lettered"] is True
    assert files["broken.pdf"]["failed_stage"] == "normalize"
    # Verbatim: a paraphrased error is a second bug to debug.
    assert "InputFileError" in files["broken.pdf"]["last_error"]


async def test_the_stage_chain_excludes_outcomes(client, signed_in) -> None:
    """`duplicate` and `failed` are ends, not steps.

    Putting them in the chain would imply every file passes through them.
    """
    await signed_in()
    body = (await client.get("/api/pipeline/files")).json()
    assert body["stages"] == [
        "received", "normalizing", "paging", "segmenting", "processed"
    ]


async def test_progress_does_not_leak_other_libraries(
    client, session, signed_in, user_factory
) -> None:
    _, _library = await signed_in()
    _user, elsewhere = await user_factory()
    session.add(
        SourceFile(
            library_id=elsewhere.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
            original_filename="not-yours.pdf", ingest_source=IngestSource.WEB_UPLOAD,
            state=SourceFileState.PROCESSED,
        )
    )
    await session.commit()

    body = (await client.get("/api/pipeline/files")).json()
    assert all(f["original_filename"] != "not-yours.pdf" for f in body["files"])


# --------------------------------------------------------------------------
# A job that is retrying is not a job that is fine
# --------------------------------------------------------------------------


async def test_a_retrying_job_needs_attention(client, session, signed_in) -> None:
    """`queue.fail` puts a job that will retry back to QUEUED with its error.

    Selecting only DEAD_LETTER and FAILED therefore reported "nothing failed"
    while two real tax documents were failing on a loop — the pipeline screen's
    single job is to make that impossible.
    """
    from api.db.enums import JobStage, JobState
    from api.db.models import Job

    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="1099.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.flush()
    session.add(
        Job(
            source_file_id=source_file.id, stage=JobStage.NORMALIZE,
            state=JobState.QUEUED, attempts=3,
            last_error="CommandError('ocrmypdf exited 1: ColorConversionNeededError')",
        )
    )
    await session.commit()

    body = (await client.get("/api/pipeline")).json()
    stages = [job["stage"] for job in body["attention"]]
    assert "normalize" in stages, "a job retrying after three failures is not idle"
    assert any("ColorConversionNeededError" in (j["last_error"] or "") for j in body["attention"])


async def test_a_healthy_queued_job_is_not_flagged(client, session, signed_in) -> None:
    """Work waiting its turn is the normal case and must stay quiet."""
    from api.db.enums import JobStage, JobState
    from api.db.models import Job

    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="fresh.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.flush()
    session.add(
        Job(source_file_id=source_file.id, stage=JobStage.NORMALIZE, state=JobState.QUEUED)
    )
    await session.commit()

    body = (await client.get("/api/pipeline")).json()
    assert body["attention"] == []


# --------------------------------------------------------------------------
# An unscoped log line is not a public one (T-10.14, REQ-144)
# --------------------------------------------------------------------------


async def test_a_line_with_no_library_is_not_visible_to_everyone(
    session, client, signed_in
) -> None:
    """The NULL branch was a leak of a different shape from a document.

    On the deployed archive it held import folder paths, login addresses, and
    the text of other people's Q&A questions — all readable by any signed-in
    user.
    """
    from api.db.models import EventLog

    _me, _library = await signed_in()
    session.add_all([
        EventLog(
            level="info", logger="bindery.import", message="scanned /data/inbox/someone",
        ),
        EventLog(
            level="info", logger="bindery.worker", message="office converter ready",
        ),
    ])
    await session.commit()

    body = (await client.get("/api/logs")).json()
    messages = [entry["message"] for entry in body["entries"]]

    assert "office converter ready" in messages, "machinery is still diagnosable"
    assert not any("someone" in message for message in messages)


async def test_your_own_unscoped_lines_are_visible_to_you(
    session, client, signed_in
) -> None:
    from api.db.models import EventLog

    me, _library = await signed_in()
    session.add(
        EventLog(
            level="info", logger="bindery.import", user_id=me.id,
            message="scanned /data/inbox/mine: 13 files",
        )
    )
    await session.commit()

    body = (await client.get("/api/logs")).json()
    assert any("mine" in entry["message"] for entry in body["entries"])


async def test_another_person_s_unscoped_lines_are_not(
    session, client, signed_in, user_factory
) -> None:
    from api.db.models import EventLog

    await signed_in()
    other, _ = await user_factory()
    session.add(
        EventLog(
            level="info", logger="bindery.import", user_id=other.id,
            message="scanned /data/inbox/theirs: 13 files",
        )
    )
    await session.commit()

    body = (await client.get("/api/logs")).json()
    assert not any("theirs" in entry["message"] for entry in body["entries"])


def test_the_answer_log_line_does_not_carry_the_question() -> None:
    """A Q&A query is among the most revealing things a person types."""
    import inspect

    from api import ai_ask

    source = inspect.getsource(ai_ask.ClaudeAnswerer.answer)
    assert "request.question[:60]" not in source
    assert "len(request.question)" in source


def test_the_auth_log_lines_do_not_carry_the_address() -> None:
    """`login_attempt` holds it and is admin-scoped; these lines are not."""
    import inspect

    from api.auth import throttle

    source = inspect.getsource(throttle.record)
    assert "login failed from %s" in source
    assert "login failed for %s" not in source


def test_the_operational_allowlist_is_an_allowlist() -> None:
    """A logger added next year and forgotten should become invisible — a
    diagnostic gap — rather than public, which would be a disclosure."""
    from api.routers.logs import OPERATIONAL_LOGGERS

    for handles_documents in ("bindery.ask", "bindery.import", "bindery.auth",
                              "bindery.accounts", "bindery.search"):
        assert handles_documents not in OPERATIONAL_LOGGERS
