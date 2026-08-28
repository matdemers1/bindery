"""T-1.10 / REQ-070 — nothing fails silently."""

import uuid

import pytest

from api import queue
from api.db.enums import IngestSource, JobStage, JobState, MembershipRole
from api.db.models import SourceFile


@pytest.fixture
async def failed_job(session, signed_in):
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id,
        sha256=uuid.uuid4().hex * 2,
        byte_size=1,
        original_filename="unreadable.pdf",
        ingest_source=IngestSource.WATCHED_FOLDER,
    )
    session.add(source_file)
    await session.flush()
    job_id = await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    await queue.fail(session, job_id, attempts=queue.MAX_ATTEMPTS, error="ghostscript exploded")
    await session.commit()
    return library, source_file, job_id


async def test_a_failed_document_is_visible(client, failed_job) -> None:
    """The forced-failure check: it has to show up somewhere a human will look."""
    _, _, job_id = failed_job

    body = (await client.get("/api/pipeline")).json()

    attention = {job["id"]: job for job in body["attention"]}
    assert str(job_id) in attention
    assert attention[str(job_id)]["state"] == "dead_letter"
    assert "ghostscript exploded" in attention[str(job_id)]["last_error"]


async def test_counts_are_grouped_by_stage_and_state(client, failed_job) -> None:
    body = (await client.get("/api/pipeline")).json()
    assert any(
        row["stage"] == "normalize" and row["state"] == "dead_letter" and row["count"] >= 1
        for row in body["counts"]
    )


async def test_pipeline_requires_authentication(client) -> None:
    await client.post("/api/auth/logout")
    assert (await client.get("/api/pipeline")).status_code == 401


async def test_pipeline_is_library_scoped(client, failed_job, signed_in) -> None:
    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")

    body = (await client.get("/api/pipeline")).json()
    assert body["attention"] == []
    assert body["counts"] == []


async def test_retry_puts_a_dead_lettered_job_back_on_the_queue(client, failed_job) -> None:
    _, _, job_id = failed_job

    body = (await client.post(f"/api/pipeline/jobs/{job_id}/retry")).json()

    assert body["state"] == JobState.QUEUED.value
    assert body["attempts"] == 0
    assert body["last_error"] is None


async def test_retry_of_someone_else_s_job_is_a_404(client, failed_job, signed_in) -> None:
    _, _, job_id = failed_job
    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")

    assert (await client.post(f"/api/pipeline/jobs/{job_id}/retry")).status_code == 404


async def test_a_reader_cannot_retry(client, failed_job, session, signed_in) -> None:
    _, _, job_id = failed_job
    await client.post("/api/auth/logout")
    await signed_in(library_name="Read Only", role=MembershipRole.READER)

    # Reader has no writable library at all, so the answer is 403 before scoping.
    assert (await client.post(f"/api/pipeline/jobs/{job_id}/retry")).status_code == 403
