"""A replay must not stop at a stage that decides it has nothing to do
(ADR-002), and a metadata field nobody needs must not cost the whole file.

Both are the same failure dressed differently: a stage returns early for a good
reason and the pipeline behind it never hears about the new bytes.
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api.audit import record
from api.db.enums import ActorType, IngestSource, JobStage, JobState, SourceFileState
from api.db.models import Job, SourceFile
from api.queue import ClaimedJob
from worker.media import parse_probe
from worker.stages.segment import run_segment


@pytest.fixture
async def hand_segmented(session, signed_in):
    """A bundle a person cut by hand — a DD-214 in a stack of scans."""
    _, library = await signed_in()
    payload = b"%PDF-" + uuid.uuid4().bytes
    source_file = SourceFile(
        library_id=library.id, sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload), original_filename="bundle.pdf",
        ingest_source=IngestSource.WEB_UPLOAD, page_count=12,
        state=SourceFileState.NORMALIZING,
    )
    session.add(source_file)
    await session.flush()
    await record(
        session, entity_type="source_file", entity_id=source_file.id,
        action="segment", actor_type=ActorType.HUMAN,
    )
    await session.commit()
    return source_file


def _job(source_file_id) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(), stage=JobStage.SEGMENT, source_file_id=source_file_id,
        document_id=None, prompt_version=None, attempts=1,
    )


async def _embed_job(session, source_file_id) -> Job | None:
    return (
        await session.execute(
            sa.select(Job).where(
                Job.stage == JobStage.EMBED.value, Job.source_file_id == source_file_id
            )
        )
    ).scalar_one_or_none()


async def test_a_hand_segmented_file_still_reaches_embed(session, hand_segmented) -> None:
    """Leaving the boundaries alone is not a reason to leave it unindexed.

    The stage refuses to re-cut a file a person segmented — correctly. It also
    returned before the cascade, so a rescan re-OCR'd the bundle, wrote the new
    text into `page.text`, and stopped: the embedding still described the old
    text and no classify or rules job was ever created for it.
    """
    await run_segment(session, _job(hand_segmented.id))
    await session.commit()

    assert await _embed_job(session, hand_segmented.id) is not None, (
        "the replay stopped at segment and nothing downstream saw the new text"
    )
    await session.refresh(hand_segmented)
    assert hand_segmented.state == SourceFileState.PROCESSED


async def test_the_second_run_resets_the_embed_job_rather_than_skipping_it(
    session, hand_segmented
) -> None:
    """`requeue_stage`, not `enqueue` — the two differ only on a replay."""
    await run_segment(session, _job(hand_segmented.id))
    await session.commit()

    job = await _embed_job(session, hand_segmented.id)
    job.state = JobState.SUCCEEDED.value
    await session.commit()

    await run_segment(session, _job(hand_segmented.id))
    await session.commit()

    await session.refresh(job)
    assert job.state == JobState.QUEUED.value, (
        "the succeeded job was left alone, so the re-OCR'd text is never embedded"
    )
    assert (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(
                Job.stage == JobStage.EMBED.value,
                Job.source_file_id == hand_segmented.id,
            )
        )
    ).scalar_one() == 1, "a replay added a second job instead of resetting the one"


# --------------------------------------------------------------------------
# ffprobe (Phase 18)
# --------------------------------------------------------------------------


def test_a_duration_ffprobe_could_not_determine_does_not_lose_the_video() -> None:
    """ffprobe prints the literal string "N/A", and "N/A" is truthy.

    A stream copy with no container duration, or a truncated recording, raised
    `ValueError: could not convert string to float: 'N/A'` inside the parse.
    `_absorb_video` is the only path a video takes, so the clip retried five
    times, dead-lettered, and never got a document, a poster or a place in the
    archive — for a field that is decoration on a card.
    """
    probed = parse_probe(
        {
            "format": {"duration": "N/A", "tags": {"creation_time": "2023-07-04T18:02:11Z"}},
            "streams": [
                {"codec_type": "video", "codec_name": "h264",
                 "width": "N/A", "height": "N/A", "duration": "N/A",
                 "avg_frame_rate": "30/1"},
            ],
        },
        "lake.mp4",
    )

    assert probed.duration_seconds is None
    assert probed.width is None and probed.height is None
    assert probed.codec == "h264"
    assert probed.captured_at is not None
    assert "video" in probed.summary()


def test_a_container_that_cannot_say_falls_back_to_the_stream() -> None:
    probed = parse_probe(
        {
            "format": {"duration": "N/A"},
            "streams": [{"codec_type": "video", "duration": "12.5", "width": 1920,
                         "height": 1080}],
        },
        "clip.mov",
    )
    assert probed.duration_seconds == 12.5
    assert (probed.width, probed.height) == (1920, 1080)


def test_an_unreadable_rotation_tag_is_ignored_rather_than_fatal() -> None:
    probed = parse_probe(
        {
            "format": {"duration": "3.0"},
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080,
                         "tags": {"rotate": "N/A"}}],
        },
        "clip.mp4",
    )
    assert (probed.width, probed.height) == (1920, 1080)
