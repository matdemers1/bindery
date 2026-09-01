"""The health panel counts what you can reach (REQ-170, ADR-009).

`collect` accepted a `library_ids` argument and never used it — every query was
global — so `/api/health/panel` called the permission helper, discarded the
answer, and reported the whole archive to everyone.

Not a content leak: the payload is counts, job ids and worker ids, with no
titles, filenames or page text, and the leak suite covers the route. The damage
was to *signal*. A badge lit by three dead letters in a library you cannot open
is a warning you have no way to answer, which is the same failure as a badge
that is always on.

`None` still means the whole archive, and that is the system watching itself:
the worker's health monitor and the notifier pass no scope, so a stall in
someone else's library is still noticed by the thing whose job it is.
"""

import uuid

import pytest
import sqlalchemy as sa

from api import health_panel
from api.db.enums import IngestSource, JobStage, JobState
from api.db.models import Job, Library, SourceFile


@pytest.fixture(autouse=True)
async def no_jobs(session):
    await session.execute(sa.delete(Job))
    await session.commit()


async def _dead_letter_in(session, library_id, *, filename="thing.pdf"):
    source = SourceFile(
        library_id=library_id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename=filename, ingest_source=IngestSource.WEB_UPLOAD,
    )
    session.add(source)
    await session.flush()
    session.add(
        Job(source_file_id=source.id, stage=JobStage.NORMALIZE,
            state=JobState.DEAD_LETTER, attempts=5, last_error="TimeoutError()")
    )
    await session.commit()


async def _other_library(session) -> uuid.UUID:
    library = Library(name=f"Someone else {uuid.uuid4().hex[:6]}", kind="personal")
    session.add(library)
    await session.flush()
    await session.commit()
    return library.id


async def test_a_failure_next_door_does_not_light_your_badge(session, signed_in):
    """The bug, stated as the thing it caused."""
    _, mine = await signed_in()
    await _dead_letter_in(session, await _other_library(session))

    panel = await health_panel.collect(session, [mine.id])
    assert panel.dead_letter == 0
    assert panel.healthy is True


async def test_your_own_failure_still_does(session, signed_in):
    """Scoping must not become silence."""
    _, mine = await signed_in()
    await _dead_letter_in(session, mine.id)

    panel = await health_panel.collect(session, [mine.id])
    assert panel.dead_letter == 1
    assert panel.healthy is False


async def test_no_scope_still_means_the_whole_archive(session, signed_in):
    """What the worker's health monitor and the notifier pass. A stall in a
    library nobody is looking at must still be noticed by something."""
    _, mine = await signed_in()
    await _dead_letter_in(session, mine.id)
    await _dead_letter_in(session, await _other_library(session))

    assert (await health_panel.collect(session)).dead_letter == 2
    assert (await health_panel.collect(session, [mine.id])).dead_letter == 1


async def test_the_route_passes_the_scope_it_computes(client, session, signed_in):
    """It used to call `_visible` and throw the answer away."""
    _, mine = await signed_in()
    await _dead_letter_in(session, await _other_library(session))

    body = (await client.get("/api/health/panel")).json()
    assert body["dead_letter"] == 0, "the route is reporting libraries the caller cannot see"

    await _dead_letter_in(session, mine.id)
    assert (await client.get("/api/health/panel")).json()["dead_letter"] == 1


async def test_every_count_is_scoped_not_just_the_alerting_one(session, signed_in):
    """Eight queries, and a fix that reached only the one being complained
    about would leave the rest quietly global."""
    _, mine = await signed_in()
    elsewhere = await _other_library(session)
    for _ in range(3):
        await _dead_letter_in(session, elsewhere)

    panel = await health_panel.collect(session, [mine.id])
    assert panel.dead_letter == 0
    assert panel.declined == 0
    assert panel.failed_24h == 0
    assert panel.stuck_jobs == []
    assert sum(panel.queue_depth.values()) == 0
    assert panel.files_by_state.get("received", 0) == 0, (
        "file counts are scoped by library too"
    )


async def test_spend_follows_the_documents_it_was_spent_on(session, signed_in):
    """A household should not be shown another household's API usage."""
    _, mine = await signed_in()
    panel = await health_panel.collect(session, [mine.id])
    assert panel.spend_30d_usd >= 0.0
