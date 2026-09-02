"""The sidebar badge asks a cheap question, and gets the same answer (CR-030).

`Shell` wraps every screen and its badges follow the `jobs` topic, which the
worker publishes on every state change of every stage of every file. That made
the two most expensive reads in the application — the full health panel, and the
review queue with its first twenty-five documents attached — into the most
frequently requested ones, several times a second for the length of an import,
to render a number and an exclamation mark.

`/api/health/badge` answers the same two questions from two indexed counts. The
risk in doing that is drift: a second definition of `healthy` that quietly stops
agreeing with the panel behind it. So most of this file is parity — condition by
condition, the badge and the panel must say the same thing — and the rest
measures the saving, so that "cheap" is a number rather than a claim.
"""

import contextlib
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from api import health_panel
from api.db.enums import (
    IngestSource,
    JobStage,
    JobState,
    ReviewState,
    SourceFileState,
)
from api.db.models import Document, Job, SourceFile
from api.db.session import engine


@pytest.fixture(autouse=True)
async def no_jobs(session):
    """Every other test's leftovers are this test's health problem."""
    await session.execute(sa.delete(Job))
    await session.commit()


async def _source_file(session, library_id) -> SourceFile:
    payload = b"%PDF-" + uuid.uuid4().bytes
    source = SourceFile(
        library_id=library_id,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        original_filename="thing.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    return source


async def _job(session, library_id, **fields) -> None:
    source = await _source_file(session, library_id)
    session.add(Job(source_file_id=source.id, stage=JobStage.NORMALIZE, **fields))
    await session.commit()


async def _assert_agrees(session, library_id) -> bool:
    """The whole point: one answer, two ways of arriving at it."""
    panel = await health_panel.collect(session, [library_id])
    badge = await health_panel.badge_healthy(session, [library_id])
    assert badge is panel.healthy, (
        f"the badge says healthy={badge} and the panel says {panel.healthy}; "
        f"alerts: {[(a.severity, a.code) for a in panel.alerts]}"
    )
    return badge


# --------------------------------------------------------------------------
# Parity — every condition that can turn the light on
# --------------------------------------------------------------------------


async def test_a_quiet_archive_agrees(session, signed_in):
    _, library = await signed_in()
    assert await _assert_agrees(session, library.id) is True


async def test_an_unacknowledged_dead_letter_agrees(session, signed_in):
    _, library = await signed_in()
    await _job(session, library.id, state=JobState.DEAD_LETTER, attempts=5)
    assert await _assert_agrees(session, library.id) is False


async def test_an_acknowledged_dead_letter_agrees(session, signed_in):
    """Acknowledging hides nothing and must not be the thing that drifts."""
    _, library = await signed_in()
    await _job(
        session, library.id, state=JobState.DEAD_LETTER, attempts=5,
        acknowledged_at=datetime.now(UTC),
    )
    assert await _assert_agrees(session, library.id) is True


async def test_a_stuck_lock_agrees(session, signed_in):
    _, library = await signed_in()
    await _job(
        session, library.id, state=JobState.RUNNING, attempts=1,
        locked_by="worker-1",
        locked_at=datetime.now(UTC) - health_panel.STALE_LOCK - timedelta(minutes=1),
    )
    assert await _assert_agrees(session, library.id) is False


async def test_a_lock_held_for_a_reasonable_time_agrees(session, signed_in):
    _, library = await signed_in()
    await _job(
        session, library.id, state=JobState.RUNNING, attempts=1,
        locked_by="worker-1", locked_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert await _assert_agrees(session, library.id) is True


async def test_a_stalled_queue_agrees(session, signed_in):
    """Queued for a long time with nothing running — the silent failure."""
    _, library = await signed_in()
    await _job(
        session, library.id, state=JobState.QUEUED,
        scheduled_for=datetime.now(UTC) - health_panel.STALL_AFTER - timedelta(minutes=1),
    )
    assert await _assert_agrees(session, library.id) is False


async def test_an_old_queue_that_is_moving_agrees(session, signed_in):
    """Not stalled: something is running, so the queue is being worked."""
    _, library = await signed_in()
    await _job(
        session, library.id, state=JobState.QUEUED,
        scheduled_for=datetime.now(UTC) - health_panel.STALL_AFTER - timedelta(minutes=1),
    )
    await _job(
        session, library.id, state=JobState.RUNNING, attempts=1,
        locked_by="worker-1", locked_at=datetime.now(UTC),
    )
    assert await _assert_agrees(session, library.id) is True


async def test_a_failure_next_door_does_not_light_the_badge(session, signed_in, user_factory):
    """The badge is scoped exactly as the panel is (REQ-170, ADR-009)."""
    _, mine = await signed_in()
    _, theirs = await user_factory(library_name="Someone else")
    await _job(session, theirs.id, state=JobState.DEAD_LETTER, attempts=5)

    assert await health_panel.badge_healthy(session, [mine.id]) is True
    # And the system watching itself, which passes no scope, still sees it.
    assert await health_panel.badge_healthy(session) is False


# --------------------------------------------------------------------------
# Parity — the number
# --------------------------------------------------------------------------


async def _document(session, library, **fields) -> Document:
    source = await _source_file(session, library.id)
    document = Document(
        library_id=library.id, source_file_id=source.id, page_start=1, page_end=1,
        **fields,
    )
    session.add(document)
    await session.commit()
    return document


async def test_the_count_is_the_one_the_review_screen_will_show(
    client, session, signed_in
):
    """A badge that disagrees with the screen behind it is worse than none."""
    user, library = await signed_in()
    await _document(session, library, review_state=ReviewState.NEEDS_REVIEW)
    await _document(session, library, review_state=ReviewState.FILED)
    # Kept out of the daily queue by default (R-03), so it must not be counted.
    await _document(session, library, review_state=ReviewState.NEEDS_REVIEW, is_backlog=True)
    # Vaulted documents are out of every ordinary view, counts included.
    await _document(
        session, library, review_state=ReviewState.NEEDS_REVIEW, vaulted_by=user.id
    )

    badge = (await client.get("/api/health/badge")).json()
    queue = (await client.get("/api/review")).json()
    assert badge["review_total"] == queue["total"] == 1


async def test_a_member_of_nothing_gets_zero_rather_than_a_refusal(
    client, session, user_factory
):
    """No membership yet is not an error state; it is an empty archive."""
    from tests.conftest import PASSWORD

    user, _ = await user_factory()
    await session.execute(
        sa.text("DELETE FROM membership WHERE user_id = :uid"), {"uid": user.id}
    )
    await session.commit()
    await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})

    response = await client.get("/api/health/badge")
    assert response.status_code == 200
    assert response.json() == {"review_total": 0, "healthy": True}


# --------------------------------------------------------------------------
# The saving, measured
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _counting_statements():
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        # `pool_pre_ping` fires a bare liveness probe on checkout, which is not
        # work the endpoint asked for.
        if statement.strip().lower() not in {"select 1", "select 1;"}:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_the_badge_costs_a_fraction_of_what_it_replaced(
    client, session, signed_in
):
    """Before and after, on the same archive, in the same run.

    Not a timing assertion — those are flaky and machine-dependent. Statements
    issued and bytes returned are what actually differ, and they differ by
    enough that a threshold does not have to be delicate.
    """
    _, library = await signed_in()
    for _ in range(5):
        await _document(session, library, review_state=ReviewState.NEEDS_REVIEW)
    await _job(session, library.id, state=JobState.QUEUED)

    with _counting_statements() as before:
        panel = await client.get("/api/health/panel")
        queue = await client.get("/api/review")
    with _counting_statements() as after:
        badge = await client.get("/api/health/badge")

    assert panel.status_code == queue.status_code == badge.status_code == 200
    was = len(before)
    now = len(after)
    print(
        f"\nbadge: {now} statements / {len(badge.content)} bytes "
        f"(was {was} / {len(panel.content) + len(queue.content)})"
    )
    assert now < was / 2, (
        f"the badge issued {now} statements against the {was} it replaced — "
        "the cheap read has stopped being cheap"
    )
    assert len(badge.content) * 4 < len(panel.content) + len(queue.content)


async def test_the_visibility_subquery_is_evaluated_once(client, session, signed_in):
    """The specific cost that made this a High.

    Every count in the panel carries `Job.id IN (job ⋈ source_file ⋈ document)`,
    and there are six of them over a table that grows to six figures during a
    backlog import and is never pruned. The badge evaluates that join once.
    """
    _, library = await signed_in()
    await _job(session, library.id, state=JobState.QUEUED)

    with _counting_statements() as statements:
        assert (await client.get("/api/health/badge")).status_code == 200

    over_jobs = [text for text in statements if "FROM job" in text]
    assert len(over_jobs) == 1, (
        "the badge reads the job table more than once:\n" + "\n---\n".join(over_jobs)
    )
