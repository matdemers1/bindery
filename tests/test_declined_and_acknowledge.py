"""Declined work, and acknowledging what genuinely failed (T-14.1 to T-14.4).

The Trust tab carried a red `!` from Phase 9 until ADR-011, and it could not be
dismissed or cleared. It was driven by `HealthPanel.healthy`, false whenever any
alert is critical, and the only critical alert was `dead_letter: 3` — a 10x5
pixel image, a 1500x10 pixel image, and a dynamic XFA form.

All three raised `PermanentFailure`. The pipeline looked at three things that
are not documents and correctly declined them. A warning that is always on is
not a warning, so the fix is to stop calling a correct answer a fault.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from api import health_panel, queue
from api.db.enums import IngestSource, JobStage, JobState
from api.db.models import Job, SourceFile


@pytest.fixture(autouse=True)
async def no_jobs(session):
    await session.execute(sa.delete(Job))
    await session.commit()


async def _job(session, library_id, *, state, error=None, acknowledged=False):
    source = SourceFile(
        library_id=library_id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="thing.png", ingest_source=IngestSource.WEB_UPLOAD,
    )
    session.add(source)
    await session.flush()
    job = Job(
        source_file_id=source.id, stage=JobStage.NORMALIZE, state=state,
        attempts=1, last_error=error,
        acknowledged_at=datetime.now(UTC) if acknowledged else None,
    )
    session.add(job)
    await session.commit()
    return job


async def test_a_refusal_never_reads_as_a_failure(session, signed_in):
    """The whole point. Three declined files and nothing else wrong is a
    healthy archive, not an alarming one."""
    _, library = await signed_in()
    await _job(session, library.id, state=JobState.DECLINED,
               error="PermanentFailure('10x5 pixels is too small')")

    panel = await health_panel.collect(session)
    assert panel.declined == 1
    assert panel.dead_letter == 0
    assert not [a for a in panel.alerts if a.code == "dead_letter"]
    assert panel.healthy is True


async def test_declined_work_is_still_reported(session, signed_in):
    """Excluded from alarms, never hidden. "Why did that file not appear?" has
    to remain answerable."""
    _, library = await signed_in()
    await _job(session, library.id, state=JobState.DECLINED, error="PermanentFailure('x')")
    assert (await health_panel.collect(session)).declined == 1


async def test_a_genuine_dead_letter_still_raises_a_critical_alert(session, signed_in):
    """The alert that catches a stuck pipeline must survive this change —
    it is what invariant 8 exists for."""
    _, library = await signed_in()
    await _job(session, library.id, state=JobState.DEAD_LETTER, error="TimeoutError()")

    panel = await health_panel.collect(session)
    assert panel.dead_letter == 1
    alert = next(a for a in panel.alerts if a.code == "dead_letter")
    assert alert.severity == "critical"
    assert "acknowledge" in alert.message


async def test_acknowledging_stops_it_counting(session, signed_in):
    _, library = await signed_in()
    await _job(session, library.id, state=JobState.DEAD_LETTER,
               error="TimeoutError()", acknowledged=True)

    panel = await health_panel.collect(session)
    assert panel.dead_letter == 0
    assert not [a for a in panel.alerts if a.code == "dead_letter"]


async def test_acknowledging_deletes_nothing(client, session, signed_in):
    """REQ-090. The row, its state and its error all survive — acknowledging is
    the weakest action available, and that is deliberate."""
    _, library = await signed_in()
    job = await _job(session, library.id, state=JobState.DEAD_LETTER, error="TimeoutError()")

    response = await client.post(f"/api/pipeline/jobs/{job.id}/acknowledge")
    assert response.status_code == 200

    await session.refresh(job)
    assert job.acknowledged_at is not None
    assert job.state is JobState.DEAD_LETTER, "acknowledging must not change the outcome"
    assert job.last_error == "TimeoutError()", "the reason must survive"


async def test_an_acknowledgement_can_be_taken_back(client, session, signed_in):
    _, library = await signed_in()
    job = await _job(session, library.id, state=JobState.DEAD_LETTER, error="TimeoutError()")

    await client.post(f"/api/pipeline/jobs/{job.id}/acknowledge")
    await client.post(f"/api/pipeline/jobs/{job.id}/acknowledge?undo=true")

    await session.refresh(job)
    assert job.acknowledged_at is None


async def test_declined_work_cannot_be_acknowledged(client, session, signed_in):
    """There is nothing to acknowledge: it was never counted. Refusing keeps
    the count honest rather than letting anything be silenced."""
    _, library = await signed_in()
    job = await _job(session, library.id, state=JobState.DECLINED, error="PermanentFailure('x')")

    response = await client.post(f"/api/pipeline/jobs/{job.id}/acknowledge")
    assert response.status_code == 409
    assert "declined" in response.text


async def test_a_running_job_cannot_be_acknowledged(client, session, signed_in):
    _, library = await signed_in()
    job = await _job(session, library.id, state=JobState.RUNNING)
    assert (await client.post(f"/api/pipeline/jobs/{job.id}/acknowledge")).status_code == 409


async def test_the_acknowledgement_is_audited(client, session, signed_in):
    from api.db.models import AuditEvent

    _, library = await signed_in()
    job = await _job(session, library.id, state=JobState.DEAD_LETTER, error="TimeoutError()")
    await client.post(f"/api/pipeline/jobs/{job.id}/acknowledge")

    event = (
        await session.execute(
            sa.select(AuditEvent)
            .where(AuditEvent.action == "acknowledge_job", AuditEvent.entity_id == job.id)
        )
    ).scalars().first()
    assert event is not None
    assert event.after["acknowledged_at"]


async def test_the_migration_reclassified_the_real_refusals(session, signed_in):
    """The three that have been lighting the badge since Phase 9 are matched by
    their error text, which is the only surviving record of why they stopped."""
    _, library = await signed_in()
    for error in (
        "PermanentFailure('10x5 pixels is too small to be a document page')",
        "PermanentFailure('1500x10 pixels is too small to be a document page')",
        "PermanentFailure('this is a dynamic XFA form built with Adobe LiveCycle')",
    ):
        state = await queue.fail(
            session,
            (await _job(session, library.id, state=JobState.QUEUED)).id,
            1, error, permanent=True,
        )
        assert state is JobState.DECLINED

    panel = await health_panel.collect(session)
    assert panel.declined == 3
    assert panel.healthy is True, "three correct refusals are not an unhealthy archive"


async def test_acknowledging_tells_the_badge(client, session, signed_in, monkeypatch):
    """The action exists to turn the badge off, so it has to say so.

    Without a published hint the sidebar refetches on its own timer — a minute
    — and keeps warning about something you have just answered. Long enough to
    read as "it did not work", which is how people learn to stop pressing the
    button (ADR-011). It also made the e2e badge assertion flaky, which is the
    same defect wearing a different hat.
    """
    from api import events

    announced: list[list] = []

    async def spy(session_, topics, **kwargs):
        announced.append(list(topics))

    monkeypatch.setattr(events, "publish", spy)

    _, library = await signed_in()
    job = await _job(session, library.id, state=JobState.DEAD_LETTER, error="TimeoutError()")

    response = await client.post(f"/api/pipeline/jobs/{job.id}/acknowledge")
    assert response.status_code == 200
    assert [events.Topic.JOBS] in announced, (
        "acknowledging published nothing, so the badge only clears on the "
        "client's fallback timer"
    )
