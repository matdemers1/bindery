"""Segmentation: turning a source file into documents, reversibly.

A segmentation is a **whole-file operation**. Replacing the set is one
transaction with one audit event, which is what makes undo (REQ-037) a flag flip
rather than a reconstruction — and what keeps the exclusion constraint happy,
since the old ranges are superseded before the new ones are inserted.

Nothing here deletes a document. Superseded rows stay, carrying the id of the
audit event that retired them, so the history of how a bundle was cut is as
durable as the cut itself.
"""

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.db.enums import ActorType, IngestSource, ReviewState
from api.db.models import Document, Page, SourceFile


class SegmentationError(ValueError):
    """The proposed segment set is not a valid cover of the file."""


@dataclass(frozen=True)
class SegmentSpec:
    page_start: int
    page_end: int
    title: str | None = None


def live() -> sa.ColumnElement[bool]:
    """The filter every read of current segments must apply."""
    return Document.superseded_at.is_(None)


async def list_segments(session: AsyncSession, source_file_id: uuid.UUID) -> list[Document]:
    result = await session.execute(
        sa.select(Document)
        .where(Document.source_file_id == source_file_id, live())
        .order_by(Document.page_start)
    )
    return list(result.scalars().all())


def validate(specs: list[SegmentSpec], page_count: int) -> None:
    """Reject a set that is not a clean, gapless, non-overlapping cover.

    The database refuses overlaps on its own; gaps it cannot see. A page that
    belongs to no document is a page that has quietly become unfindable by
    document, which is precisely the failure this project exists to prevent — so
    it is rejected here with an explanation rather than accepted silently.
    """
    if not specs:
        raise SegmentationError("a file must have at least one segment")

    ordered = sorted(specs, key=lambda spec: spec.page_start)

    # Bounds first: "pages 1-20 fall outside 1-10" says more than "the last
    # segment must end at page 10" when someone has simply mistyped a number.
    for spec in ordered:
        if spec.page_end < spec.page_start:
            raise SegmentationError(
                f"segment {spec.page_start}-{spec.page_end} ends before it starts"
            )
        if spec.page_start < 1 or spec.page_end > page_count:
            raise SegmentationError(
                f"pages {spec.page_start}-{spec.page_end} fall outside 1-{page_count}"
            )

    if ordered[0].page_start != 1:
        raise SegmentationError("the first segment must start at page 1")
    if ordered[-1].page_end != page_count:
        raise SegmentationError(f"the last segment must end at page {page_count}")

    for previous, following in itertools.pairwise(ordered):
        if following.page_start <= previous.page_end:
            raise SegmentationError(
                f"segments {previous.page_start}-{previous.page_end} and "
                f"{following.page_start}-{following.page_end} overlap"
            )
        if following.page_start != previous.page_end + 1:
            raise SegmentationError(
                f"pages {previous.page_end + 1}-{following.page_start - 1} belong to no segment"
            )


def _manifest(documents: list[Document]) -> list[dict[str, Any]]:
    """The shape stored in the audit trail, and read back by undo."""
    return [
        {
            "id": str(document.id),
            "page_start": document.page_start,
            "page_end": document.page_end,
            "title": document.title,
        }
        for document in documents
    ]


async def replace(
    session: AsyncSession,
    source_file: SourceFile,
    specs: list[SegmentSpec],
    *,
    actor_type: ActorType,
    actor_id: uuid.UUID | None = None,
    action: str = "segment",
) -> list[Document]:
    """Replace a file's segments with `specs`, in one audited operation."""
    page_count = source_file.page_count or await _count_pages(session, source_file.id)
    validate(specs, page_count)

    previous = await list_segments(session, source_file.id)

    event = await record(
        session,
        entity_type="source_file",
        entity_id=source_file.id,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        before={"segments": _manifest(previous)},
        after={"segments": [
            {"page_start": s.page_start, "page_end": s.page_end, "title": s.title}
            for s in sorted(specs, key=lambda spec: spec.page_start)
        ]},
    )
    await session.flush()

    # Retire the old set first: the exclusion constraint is deferred to neither
    # statement end nor transaction end, so the ranges must not coexist.
    if previous:
        await session.execute(
            sa.update(Document)
            .where(Document.id.in_([document.id for document in previous]))
            .values(superseded_at=datetime.now(UTC), superseded_by_event_id=event.id)
        )
        await session.flush()

    # Inherited from the file, not stamped on afterwards.
    #
    # `mark_backlog` used to do this from the import endpoint, immediately
    # after ingest — before normalize, paging and segmentation had run, so
    # there were no documents yet to mark. It flagged whatever happened to
    # exist from an earlier slice: 15 of 396 in a real import, and the other
    # 381 landed in the daily review queue, which is exactly the pile the
    # backlog flag exists to prevent (R-03).
    #
    # How a file arrived is known at ingest and never changes, so the document
    # can simply take it from the file at the moment it is created.
    from_backlog = source_file.ingest_source == IngestSource.BULK_IMPORT

    created = [
        Document(
            library_id=source_file.library_id,
            source_file_id=source_file.id,
            page_start=spec.page_start,
            page_end=spec.page_end,
            title=spec.title,
            review_state=ReviewState.PENDING_CLASSIFICATION,
            is_backlog=from_backlog,
        )
        for spec in sorted(specs, key=lambda spec: spec.page_start)
    ]
    session.add_all(created)
    await session.flush()
    return created


async def undo(
    session: AsyncSession,
    source_file: SourceFile,
    *,
    actor_id: uuid.UUID | None = None,
) -> list[Document]:
    """Restore the segment set as it was before the last segmentation.

    Reversal is symmetric: the rows retired by that event come back, and the
    rows it created are retired in turn. The source file is not read, let alone
    written — undoing a segmentation cannot touch a byte of the original.
    """
    from api.db.models import AuditEvent

    last = (
        await session.execute(
            sa.select(AuditEvent)
            .where(
                AuditEvent.entity_type == "source_file",
                AuditEvent.entity_id == source_file.id,
                AuditEvent.action.in_(("segment", "segment_undo")),
            )
            # By sequence, not timestamp: two segmentations written in one
            # transaction share a created_at, and "the last one" would then be
            # decided by a random uuid.
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if last is None:
        raise SegmentationError("this file has never been segmented")

    restored_ids = [entry["id"] for entry in (last.before or {}).get("segments", [])]
    if not restored_ids:
        raise SegmentationError("there is no earlier segmentation to return to")

    current = await list_segments(session, source_file.id)

    event = await record(
        session,
        entity_type="source_file",
        entity_id=source_file.id,
        action="segment_undo",
        actor_type=ActorType.HUMAN,
        actor_id=actor_id,
        before={"segments": _manifest(current)},
        after={"segments": (last.before or {}).get("segments", [])},
    )
    await session.flush()

    if current:
        await session.execute(
            sa.update(Document)
            .where(Document.id.in_([document.id for document in current]))
            .values(superseded_at=datetime.now(UTC), superseded_by_event_id=event.id)
        )
        await session.flush()

    await session.execute(
        sa.update(Document)
        .where(Document.id.in_([uuid.UUID(value) for value in restored_ids]))
        .values(superseded_at=None, superseded_by_event_id=None)
    )
    await session.flush()
    return await list_segments(session, source_file.id)


async def _count_pages(session: AsyncSession, source_file_id: uuid.UUID) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count()).select_from(Page).where(
                Page.source_file_id == source_file_id
            )
        )
    ).scalar_one()
