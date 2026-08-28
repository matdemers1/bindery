"""T-2.1 / T-2.3 — segments as page ranges, and the constraints that hold them.

The properties that matter: overlaps and cross-library assignments are
*structurally* impossible, and re-segmenting never touches the source file.
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from api import segments
from api.db.enums import ActorType, IngestSource, LibraryKind, SourceFileState
from api.db.models import AuditEvent, Document, Library, Page, SourceFile
from api.segments import SegmentSpec
from api.storage.blobs import blob_path

PAGE_COUNT = 10


@pytest.fixture
async def bundle(session, signed_in):
    """A ten-page file with real bytes in the blob store."""
    _, library = await signed_in()
    payload = b"%PDF-1.7\n" + uuid.uuid4().bytes * 8 + b"\n%%EOF\n"
    sha = hashlib.sha256(payload).hexdigest()
    path = blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    source_file = SourceFile(
        library_id=library.id,
        sha256=sha,
        byte_size=len(payload),
        original_filename="Bundle.pdf",
        ingest_source=IngestSource.WATCHED_FOLDER,
        page_count=PAGE_COUNT,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    for number in range(1, PAGE_COUNT + 1):
        session.add(
            Page(source_file_id=source_file.id, page_number=number, text=f"page {number} body")
        )
    await session.commit()
    return library, source_file


# --------------------------------------------------------------------------
# Constraints (REQ-032, REQ-033)
# --------------------------------------------------------------------------


async def test_overlapping_page_ranges_are_rejected_by_the_database(session, bundle) -> None:
    """REQ-032, enforced by the exclusion constraint, not by application code."""
    library, source_file = bundle
    session.add(
        Document(
            library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=5
        )
    )
    await session.flush()
    session.add(
        Document(
            library_id=library.id, source_file_id=source_file.id, page_start=5, page_end=8
        )
    )

    with pytest.raises(IntegrityError, match="ex_document_page_range_overlap"):
        await session.flush()
    await session.rollback()


async def test_adjacent_page_ranges_are_fine(session, bundle) -> None:
    """1-5 and 6-10 touch but do not overlap."""
    library, source_file = bundle
    session.add_all([
        Document(library_id=library.id, source_file_id=source_file.id, page_start=1, page_end=5),
        Document(library_id=library.id, source_file_id=source_file.id, page_start=6, page_end=10),
    ])
    await session.flush()
    assert len(await segments.list_segments(session, source_file.id)) == 2
    await session.rollback()


async def test_a_document_cannot_belong_to_another_library(session, bundle) -> None:
    """REQ-033, enforced by the composite foreign key."""
    _, source_file = bundle
    elsewhere = Library(name="Elsewhere", kind=LibraryKind.PERSONAL)
    session.add(elsewhere)
    await session.flush()

    session.add(
        Document(
            library_id=elsewhere.id, source_file_id=source_file.id, page_start=1, page_end=3
        )
    )
    with pytest.raises(IntegrityError, match="fk_document_source_file_library"):
        await session.flush()
    await session.rollback()


async def test_superseded_ranges_may_overlap_live_ones(session, bundle) -> None:
    """History has to be allowed to contradict the present, or undo is impossible."""
    _, source_file = bundle
    await segments.replace(
        session, source_file, [SegmentSpec(1, 10)], actor_type=ActorType.SYSTEM
    )
    await segments.replace(
        session,
        source_file,
        [SegmentSpec(1, 4), SegmentSpec(5, 10)],
        actor_type=ActorType.HUMAN,
    )
    await session.commit()

    live = await segments.list_segments(session, source_file.id)
    total = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Document)
            .where(Document.source_file_id == source_file.id)
        )
    ).scalar_one()

    assert [(d.page_start, d.page_end) for d in live] == [(1, 4), (5, 10)]
    # The 1-10 row still exists; it is history, not garbage.
    assert total == 3


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("specs", "message"),
    [
        ([], "at least one segment"),
        ([SegmentSpec(2, 10)], "must start at page 1"),
        ([SegmentSpec(1, 9)], "must end at page 10"),
        ([SegmentSpec(1, 3), SegmentSpec(6, 10)], "belong to no segment"),
        ([SegmentSpec(1, 6), SegmentSpec(5, 10)], "overlap"),
        ([SegmentSpec(1, 20)], "outside"),
    ],
)
def test_an_invalid_cover_is_refused_with_an_explanation(specs, message) -> None:
    """A page in no segment is a page that has quietly become unfindable."""
    with pytest.raises(segments.SegmentationError, match=message):
        segments.validate(specs, PAGE_COUNT)


# --------------------------------------------------------------------------
# Replace and undo (REQ-031, REQ-037)
# --------------------------------------------------------------------------


async def test_segmenting_never_touches_the_source_file(session, bundle) -> None:
    """REQ-031 — the whole point of ADR-001."""
    _, source_file = bundle
    path = blob_path(source_file.sha256)
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    await segments.replace(
        session,
        source_file,
        [SegmentSpec(1, 3, "Cover"), SegmentSpec(4, 7, "DD-214"), SegmentSpec(8, 10, "Orders")],
        actor_type=ActorType.HUMAN,
    )
    await session.commit()

    after = (path.read_bytes(), path.stat().st_mtime_ns)
    assert after == before
    assert hashlib.sha256(after[0]).hexdigest() == source_file.sha256


async def test_replace_records_one_audited_operation(session, bundle) -> None:
    _, source_file = bundle
    await segments.replace(
        session, source_file, [SegmentSpec(1, 10)], actor_type=ActorType.SYSTEM
    )
    await segments.replace(
        session,
        source_file,
        [SegmentSpec(1, 5), SegmentSpec(6, 10)],
        actor_type=ActorType.HUMAN,
    )
    await session.commit()

    events = (
        await session.execute(
            sa.select(AuditEvent)
            .where(AuditEvent.entity_id == source_file.id, AuditEvent.action == "segment")
            .order_by(AuditEvent.created_at)
        )
    ).scalars().all()

    assert len(events) == 2
    assert events[1].actor_type is ActorType.HUMAN
    assert len(events[1].before["segments"]) == 1
    assert len(events[1].after["segments"]) == 2


async def test_undo_restores_the_previous_set(session, bundle) -> None:
    """REQ-037 — reversal is a flag flip, not a reconstruction."""
    _, source_file = bundle
    await segments.replace(
        session, source_file, [SegmentSpec(1, 10, "Whole bundle")], actor_type=ActorType.SYSTEM
    )
    await session.commit()
    original = [(d.page_start, d.page_end, d.title, d.id) for d in
                await segments.list_segments(session, source_file.id)]

    await segments.replace(
        session,
        source_file,
        [SegmentSpec(1, 4), SegmentSpec(5, 10)],
        actor_type=ActorType.HUMAN,
    )
    await session.commit()

    restored = await segments.undo(session, source_file)
    await session.commit()

    # Same rows, not equivalent copies.
    assert [(d.page_start, d.page_end, d.title, d.id) for d in restored] == original


async def test_undo_leaves_the_blob_untouched(session, bundle) -> None:
    _, source_file = bundle
    path = blob_path(source_file.sha256)
    before = path.read_bytes()

    await segments.replace(
        session, source_file, [SegmentSpec(1, 10)], actor_type=ActorType.SYSTEM
    )
    await segments.replace(
        session, source_file, [SegmentSpec(1, 5), SegmentSpec(6, 10)], actor_type=ActorType.HUMAN
    )
    await session.commit()
    await segments.undo(session, source_file)
    await session.commit()

    assert path.read_bytes() == before


async def test_undo_without_a_prior_segmentation_is_refused(session, bundle) -> None:
    _, source_file = bundle
    # SegmentationError is raised before any failing SQL, so the transaction is
    # still usable and no rollback is needed.
    with pytest.raises(segments.SegmentationError, match="never been segmented"):
        await segments.undo(session, source_file)

    await segments.replace(
        session, source_file, [SegmentSpec(1, 10)], actor_type=ActorType.SYSTEM
    )
    await session.commit()
    # The first segmentation has nothing before it to return to.
    with pytest.raises(segments.SegmentationError, match="no earlier segmentation"):
        await segments.undo(session, source_file)


async def test_undo_is_deterministic_when_segmentations_share_a_transaction(
    session, bundle
) -> None:
    """`created_at` defaults to now(), which is transaction-scoped.

    Two segmentations written without a commit between them therefore carry the
    same timestamp, and ordering by it would pick between them at random —
    silently undoing to the wrong set. Ordering is by the audit sequence.
    """
    _, source_file = bundle
    await segments.replace(
        session, source_file, [SegmentSpec(1, 10, "First")], actor_type=ActorType.SYSTEM
    )
    await segments.replace(
        session, source_file, [SegmentSpec(1, 6, "Second"), SegmentSpec(7, 10, "Tail")],
        actor_type=ActorType.HUMAN,
    )
    await session.commit()

    events = (
        await session.execute(
            sa.select(AuditEvent.created_at, AuditEvent.sequence)
            .where(AuditEvent.entity_id == source_file.id, AuditEvent.action == "segment")
            .order_by(AuditEvent.sequence)
        )
    ).all()
    # The premise: identical timestamps, distinct sequence numbers.
    assert events[0].created_at == events[1].created_at
    assert events[0].sequence < events[1].sequence

    restored = await segments.undo(session, source_file)
    await session.commit()
    assert [(d.page_start, d.page_end, d.title) for d in restored] == [(1, 10, "First")]
