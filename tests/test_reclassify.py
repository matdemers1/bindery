"""Re-running AI review (T-8.10).

The archive is deliberately useful with no API key — OCR, indexing and search
all work — so the ordinary way to use it is to put documents in first and add a
key later. That makes "run AI review over the things that missed it" a normal
operation rather than a recovery procedure, and it had no path through the UI at
all: the only way was a shell on the host.

It was also invisible. Classify jobs carry `document_id` and no
`source_file_id`, and the pipeline screen reached jobs by an inner join through
`source_file` — so a document whose classification failed showed nothing,
anywhere, and could not be retried.
"""

import uuid

import pytest
import sqlalchemy as sa

from api import reclassify
from api.db.enums import (
    IngestSource,
    JobStage,
    JobState,
    ReviewState,
    SourceFileState,
)
from api.db.models import Classification, Document, Job, Page, SourceFile


@pytest.fixture
async def unclassified(session, signed_in):
    """Three documents, each unclassified for a different reason."""
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=100,
        original_filename="batch.pdf", ingest_source=IngestSource.WATCHED_FOLDER,
        page_count=3, state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    for n in range(1, 4):
        session.add(Page(source_file_id=source_file.id, page_number=n, text=f"page {n}"))

    never = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=1,
        review_state=ReviewState.PENDING_CLASSIFICATION,
    )
    no_key = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=2, page_end=2,
        review_state=ReviewState.PENDING_CLASSIFICATION,
    )
    broke = Document(
        library_id=library.id, source_file_id=source_file.id, page_start=3, page_end=3,
        review_state=ReviewState.PENDING_CLASSIFICATION,
    )
    session.add_all([never, no_key, broke])
    await session.flush()

    session.add_all([
        Job(
            document_id=no_key.id, stage=JobStage.CLASSIFY, state=JobState.DEAD_LETTER,
            attempts=5,
            last_error="ProviderUnavailableError('no AI provider is configured; "
                       "classification deferred')",
        ),
        Job(
            document_id=broke.id, stage=JobStage.CLASSIFY, state=JobState.DEAD_LETTER,
            attempts=5, last_error="AIProviderError('response did not match the schema')",
        ),
    ])
    await session.commit()
    return library, {"never": never, "no_key": no_key, "broke": broke}


def _reason(result, code):
    return next((r for r in result.reasons if r.code == code), None)


# --------------------------------------------------------------------------
# Telling the two situations apart
# --------------------------------------------------------------------------


async def test_never_attempted_is_not_reported_as_a_failure(session, unclassified) -> None:
    """"Waiting for the key you just added" is not "gave up", and saying so
    would be both alarming and wrong."""
    library, docs = unclassified
    result = await reclassify.pending(session, [library.id])

    never = _reason(result, "never_attempted")
    assert docs["never"].id in never.document_ids
    assert docs["never"].id not in _reason(result, "provider_unavailable").document_ids
    assert docs["never"].id not in _reason(result, "failed").document_ids


async def test_a_missing_key_is_distinguished_from_a_real_failure(
    session, unclassified
) -> None:
    library, docs = unclassified
    result = await reclassify.pending(session, [library.id])

    assert _reason(result, "provider_unavailable").document_ids == [docs["no_key"].id]
    assert _reason(result, "failed").document_ids == [docs["broke"].id]
    assert result.total == 3


async def test_the_no_key_reason_does_not_promise_a_key_that_is_not_set(
    session, unclassified
) -> None:
    """The trust surface must not assert a fact it has not checked (D-05).

    This sentence is read by somebody who is on this screen *because* nothing
    is succeeding. Telling them "now that a key is set they will succeed" on an
    archive with no key — the ordinary state, since the archive is designed to
    be useful without one — is the filing reporting a success it cannot know.
    """
    library, _docs = unclassified
    result = await reclassify.pending(session, [library.id])

    detail = _reason(result, "provider_unavailable").detail
    assert "still no key" in detail
    assert "Settings" in detail, "it has to say where to fix it"
    assert "will succeed" not in detail


async def test_the_no_key_reason_changes_once_a_key_is_set(
    session, unclassified
) -> None:
    """And it must change when the fact changes, or it is just different copy."""
    from api import settings_store

    library, _docs = unclassified
    await settings_store.set_(
        session, settings_store.ANTHROPIC_API_KEY, "sk-ant-not-a-real-key", actor_id=None
    )

    detail = _reason(
        await reclassify.pending(session, [library.id]), "provider_unavailable"
    ).detail
    assert "A key is set now" in detail
    assert "still no key" not in detail


async def test_an_already_classified_document_is_not_pending(
    session, unclassified
) -> None:
    library, docs = unclassified
    session.add(
        Classification(
            document_id=docs["never"].id, model="claude-opus-5", prompt_version="v1"
        )
    )
    await session.commit()

    result = await reclassify.pending(session, [library.id])
    assert docs["never"].id not in result.all_ids()
    assert result.total == 2


async def test_pending_is_scoped_to_the_callers_libraries(
    session, unclassified, signed_in
) -> None:
    _library, _docs = unclassified
    _, elsewhere = await signed_in()

    result = await reclassify.pending(session, [elsewhere.id])
    assert result.total == 0


# --------------------------------------------------------------------------
# Re-running
# --------------------------------------------------------------------------


async def test_requeueing_resets_a_dead_lettered_job(session, unclassified) -> None:
    library, docs = unclassified
    queued = await reclassify.requeue(session, [docs["no_key"].id], [library.id])
    await session.commit()
    assert queued == 1

    job = (
        await session.execute(
            sa.select(Job).where(Job.document_id == docs["no_key"].id)
        )
    ).scalar_one()
    assert job.state == JobState.QUEUED
    assert job.attempts == 0
    assert job.last_error is None, "a fresh attempt should not carry the old error"


async def test_requeueing_a_document_that_never_ran_creates_the_job(
    session, unclassified
) -> None:
    library, docs = unclassified
    queued = await reclassify.requeue(session, [docs["never"].id], [library.id])
    await session.commit()
    assert queued == 1

    job = (
        await session.execute(sa.select(Job).where(Job.document_id == docs["never"].id))
    ).scalar_one()
    assert job.stage == JobStage.CLASSIFY
    assert job.state == JobState.QUEUED


async def test_a_document_from_another_library_is_refused(
    session, unclassified, signed_in
) -> None:
    """An id is a capability, and this one costs money to exercise."""
    _library, docs = unclassified
    _, elsewhere = await signed_in()

    queued = await reclassify.requeue(session, [docs["no_key"].id], [elsewhere.id])
    assert queued == 0


async def test_requeueing_is_idempotent(session, unclassified) -> None:
    library, docs = unclassified
    first = await reclassify.requeue(session, [docs["never"].id], [library.id])
    await session.commit()
    second = await reclassify.requeue(session, [docs["never"].id], [library.id])
    await session.commit()
    assert first == 1 and second == 1

    count = await session.scalar(
        sa.select(sa.func.count()).select_from(Job).where(
            Job.document_id == docs["never"].id, Job.stage == JobStage.CLASSIFY
        )
    )
    assert count == 1, "pressing the button twice must not double the work"


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


async def test_the_endpoint_reports_what_is_waiting_and_why(client, unclassified) -> None:
    response = await client.get("/api/pipeline/reclassify/pending")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    codes = {reason["code"]: reason["count"] for reason in body["reasons"]}
    assert codes == {"never_attempted": 1, "provider_unavailable": 1, "failed": 1}


async def test_re_running_everything_waiting(client, session, unclassified) -> None:
    response = await client.post("/api/pipeline/reclassify", json={"all_pending": True})
    assert response.status_code == 200, response.text
    assert response.json()["queued"] == 3


async def test_re_running_only_one_reason(client, session, unclassified) -> None:
    """"Retry the ones that failed" and "review the ones that never ran" are
    separate decisions, so they are separate buttons."""
    response = await client.post(
        "/api/pipeline/reclassify",
        json={"all_pending": True, "reasons": ["provider_unavailable"]},
    )
    assert response.json()["queued"] == 1


# --------------------------------------------------------------------------
# The bug that made all of this invisible
# --------------------------------------------------------------------------


async def test_a_failed_classification_appears_on_the_pipeline_screen(
    client, unclassified
) -> None:
    """Classify jobs carry no `source_file_id`.

    The pipeline query reached jobs by an inner join through `source_file`, so
    every classification failure was silently dropped from the one screen whose
    entire purpose is that nothing fails silently — and the retry button that
    would have fixed them was never rendered.
    """
    response = await client.get("/api/pipeline")
    assert response.status_code == 200, response.text
    body = response.json()

    stages = {job["stage"] for job in body["attention"]}
    assert "classify" in stages, "a dead-lettered classification must be visible"
    assert len(body["attention"]) == 2

    classify_counts = [
        entry for entry in body["counts"] if entry["stage"] == "classify"
    ]
    assert classify_counts, "and it must be counted, not just listed"


async def test_pipeline_jobs_still_do_not_leak_across_libraries(
    client, session, unclassified, user_factory
) -> None:
    """The reachability fix widened a query. Confirm it did not widen the boundary.

    `user_factory` rather than `signed_in`: signing in swaps the client's
    identity, and the caller has to stay the original user for this to be
    testing anything.
    """
    _library, _docs = unclassified
    _, elsewhere = await user_factory()
    stranger = SourceFile(
        library_id=elsewhere.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="theirs.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(stranger)
    await session.flush()
    their_document = Document(
        library_id=elsewhere.id, source_file_id=stranger.id, page_start=1, page_end=1
    )
    session.add(their_document)
    await session.flush()
    session.add(
        Job(document_id=their_document.id, stage=JobStage.CLASSIFY,
            state=JobState.DEAD_LETTER, attempts=5, last_error="theirs")
    )
    await session.commit()

    body = (await client.get("/api/pipeline")).json()
    assert "theirs" not in str(body)
    assert all(
        job["document_id"] != str(their_document.id) for job in body["attention"]
    )


async def test_a_document_filed_without_ai_is_not_waiting_for_it(
    session, unclassified
) -> None:
    """A rule or a person can file a document, and that is a finished document.

    Treating "has no classification row" as "needs AI review" would sweep up
    everything filed by hand and offer to overwrite it.
    """
    library, docs = unclassified
    docs["never"].review_state = ReviewState.FILED
    await session.commit()

    result = await reclassify.pending(session, [library.id])
    assert docs["never"].id not in result.all_ids()


async def test_a_document_needing_human_review_is_not_waiting_for_ai(
    session, unclassified
) -> None:
    """It already had its review; what it is waiting for is you."""
    library, docs = unclassified
    docs["never"].review_state = ReviewState.NEEDS_REVIEW
    await session.commit()

    result = await reclassify.pending(session, [library.id])
    assert docs["never"].id not in result.all_ids()


# --------------------------------------------------------------------------
# Rescan — reading the file again from the original
# --------------------------------------------------------------------------


async def test_rescan_requeues_the_whole_pipeline_from_ocr(
    session, client, unclassified
) -> None:
    """"Rescan" has to mean rescan.

    Requeueing `normalize` re-runs OCR and cascades through paging,
    segmentation, embedding and classification. Requeueing anything later would
    be a partial repair wearing the word.
    """
    _library, docs = unclassified
    source_file_id = docs["never"].source_file_id

    response = await client.post(f"/api/source-files/{source_file_id}/rescan")
    assert response.status_code == 200, response.text
    assert response.json()["queued"] is True

    job = (
        await session.execute(
            sa.select(Job).where(
                Job.source_file_id == source_file_id, Job.stage == JobStage.NORMALIZE
            )
        )
    ).scalar_one()
    assert job.state == JobState.QUEUED
    assert job.attempts == 0


async def test_rescan_is_audited(session, client, unclassified) -> None:
    from api.db.models import AuditEvent

    _library, docs = unclassified
    source_file_id = docs["never"].source_file_id
    await client.post(f"/api/source-files/{source_file_id}/rescan")

    event = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == source_file_id,
                AuditEvent.action == "rescan_requested",
            )
        )
    ).scalar_one()
    assert event.after["queued"] is True


async def test_rescan_leaves_the_original_alone(session, client, unclassified) -> None:
    """Everything a rescan rebuilds is derived; the bytes are never touched."""
    _library, docs = unclassified
    source_file_id = docs["never"].source_file_id
    before = (await session.get(SourceFile, source_file_id)).sha256

    await client.post(f"/api/source-files/{source_file_id}/rescan")
    await session.commit()

    after = (await session.get(SourceFile, source_file_id)).sha256
    assert after == before


async def test_you_cannot_rescan_a_file_you_cannot_see(
    session, client, unclassified, user_factory
) -> None:
    from api.db.enums import IngestSource, SourceFileState

    _user, elsewhere = await user_factory()
    stranger = SourceFile(
        library_id=elsewhere.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="theirs.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(stranger)
    await session.commit()

    response = await client.post(f"/api/source-files/{stranger.id}/rescan")
    assert response.status_code == 404, "403 would confirm the file exists"


async def test_a_rescan_actually_reaches_the_stages_after_ocr(
    session, client, unclassified
) -> None:
    """A replay has to replay the whole chain.

    The cascade used `enqueue`, which is idempotent and refuses to disturb an
    existing job — correct for a first run, fatal for a replay. On a real
    rescan the file was re-OCR'd, recovered 262 words, and then stopped: paging
    had already succeeded once, so it never ran again and the new text was
    never indexed. The file sat in `paging` with zero characters.
    """
    from api import queue
    from api.db.enums import JobState as State

    _library, docs = unclassified
    source_file_id = docs["never"].source_file_id

    # Every downstream stage already ran once, exactly as after a first ingest.
    for stage in (JobStage.PAGE, JobStage.SEGMENT, JobStage.EMBED):
        await queue.enqueue(session, stage, source_file_id=source_file_id)
    await session.execute(
        sa.update(Job)
        .where(Job.source_file_id == source_file_id, Job.stage != JobStage.NORMALIZE)
        .values(state=State.SUCCEEDED.value, attempts=1)
    )
    await session.commit()

    # What the worker does at the end of a re-run of normalize.
    assert await queue.requeue_stage(
        session, JobStage.PAGE, source_file_id=source_file_id
    ), "requeueing a succeeded stage must reset it, not no-op"
    await session.commit()

    page_job = (
        await session.execute(
            sa.select(Job).where(
                Job.source_file_id == source_file_id, Job.stage == JobStage.PAGE
            )
        )
    ).scalar_one()
    assert page_job.state == JobState.QUEUED
    assert page_job.attempts == 0


async def test_enqueue_still_refuses_to_disturb_work_in_the_normal_flow(
    session, unclassified
) -> None:
    """The idempotency that makes `enqueue` right for a first run is intact.

    Two ingests of the same file must not produce two pipelines; only a
    deliberate replay resets anything.
    """
    from api import queue

    _library, docs = unclassified
    source_file_id = docs["never"].source_file_id

    first = await queue.enqueue(session, JobStage.PAGE, source_file_id=source_file_id)
    second = await queue.enqueue(session, JobStage.PAGE, source_file_id=source_file_id)
    await session.commit()

    assert first is not None
    assert second is None, "enqueue is still a no-op when the job exists"


# --------------------------------------------------------------------------
# A refusal is its own answer (ADR-011)
# --------------------------------------------------------------------------


@pytest.fixture
async def refused(session, unclassified):
    """A fourth document, which the model looked at and declined to classify."""
    library, docs = unclassified
    source_file_id = docs["never"].source_file_id
    session.add(Page(source_file_id=source_file_id, page_number=4, text="page 4"))
    declined = Document(
        library_id=library.id, source_file_id=source_file_id, page_start=4, page_end=4,
        review_state=ReviewState.PENDING_CLASSIFICATION,
    )
    session.add(declined)
    await session.flush()
    session.add(
        Job(
            document_id=declined.id, stage=JobStage.CLASSIFY, state=JobState.DECLINED,
            attempts=1,
            last_error="ProviderRefusedError('the model declined to classify this')",
        )
    )
    await session.commit()
    return library, declined


async def test_a_refusal_is_neither_waiting_nor_a_failure(session, refused) -> None:
    """It fell into "never attempted", under a heading that says nothing failed.

    Which was wrong twice over: something did happen, and what happened was an
    answer. The screen then offered to run AI review on it — forever, and
    identically each time.
    """
    library, declined = refused
    result = await reclassify.pending(session, [library.id])

    assert _reason(result, "declined").document_ids == [declined.id]
    for code in ("never_attempted", "provider_unavailable", "failed"):
        assert declined.id not in _reason(result, code).document_ids


async def test_the_declined_bucket_does_not_invite_a_re_run(session, refused) -> None:
    """The one property the UI reads to decide whether to render a button."""
    library, _declined = refused
    result = await reclassify.pending(session, [library.id])

    assert _reason(result, "declined").rerunnable is False
    assert all(
        _reason(result, code).rerunnable
        for code in ("never_attempted", "provider_unavailable", "failed")
    ), "the other three are still worth re-running"

    wire = result.as_dict()
    assert {r["code"]: r["rerunnable"] for r in wire["reasons"]}["declined"] is False


async def test_running_everything_waiting_leaves_the_refusals_alone(
    client, session, refused
) -> None:
    """"All" means everything a re-run could reach.

    Sweeping a refusal back in spends money to be told no a second time, and
    the count in the button would be promising work it cannot do.
    """
    _library, declined = refused
    response = await client.post("/api/pipeline/reclassify", json={"all_pending": True})
    assert response.status_code == 200, response.text
    assert response.json()["queued"] == 3, "the other three, not four"

    job = (
        await session.execute(sa.select(Job).where(Job.document_id == declined.id))
    ).scalar_one()
    assert job.state == JobState.DECLINED, "its job was not reset"


async def test_a_refusal_can_still_be_re_run_when_it_is_asked_for_by_name(
    client, session, refused
) -> None:
    """Not forbidden — just never offered.

    Naming the code is a deliberate act, and the answer genuinely can change:
    the model is selectable, and a file one model refuses another may read.
    """
    _library, declined = refused
    response = await client.post(
        "/api/pipeline/reclassify",
        json={"all_pending": True, "reasons": ["declined"]},
    )
    assert response.json()["queued"] == 1

    job = (
        await session.execute(sa.select(Job).where(Job.document_id == declined.id))
    ).scalar_one()
    assert job.state == JobState.QUEUED


async def test_a_refusal_is_counted_but_not_called_a_failure(client, refused) -> None:
    """It is still without AI review, so it is still in the total — the archive's
    own count of what has no title, date or tags must not quietly shrink."""
    body = (await client.get("/api/pipeline/reclassify/pending")).json()
    assert body["total"] == 4
    codes = {reason["code"]: reason["count"] for reason in body["reasons"]}
    assert codes == {
        "never_attempted": 1, "provider_unavailable": 1, "failed": 1, "declined": 1,
    }
