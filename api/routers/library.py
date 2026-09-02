"""Browsing the archive (screen 2).

Search answers "where is the thing I know I have". This answers the other
question — *what is in here, how did it get here, and what did the system decide
about it* — which until now had no answer at all: with an empty query the search
screen showed nothing, so a document you could not name was a document you could
not reach.

Two shapes over the same set:

- **A flat list**, filterable, showing provenance and classification side by
  side, so "what came in from the scanner last week and what did it get tagged"
  is one screen.
- **A tree**, grouped the way the mirror tree on disk will be grouped in Phase 6
  (`year / type / title`), so the browsable structure and the on-disk structure
  agree rather than being two different mental models.
"""

import uuid
from collections import defaultdict

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import events, queue
from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType, JobStage, ReviewState
from api.db.models import (
    AppUser,
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    KnownForm,
    SourceFile,
    Tag,
    live_tag_links,
)
from api.db.scope import Scope
from api.db.session import get_session
from api.schemas import (
    ArchiveEntryOut,
    ArchiveOut,
    ArchiveStatsOut,
    RescanResultOut,
    TagOut,
    TreeGroupOut,
    TreeOut,
)
from api.segments import live

router = APIRouter(tags=["library"])

SORTS = {
    "newest": (SourceFile.received_at.desc(), Document.page_start),
    "oldest": (SourceFile.received_at.asc(), Document.page_start),
    "date": (Document.document_date.desc().nullslast(), Document.created_at.desc()),
    "title": (Document.title.asc().nullslast(),),
}


def _base(bound: Scope):
    """Every live document the caller may see, with everything a row displays.

    `bound.only(Document)` is the library filter and the vault filter as one
    condition (`api/db/scope.py`): it is where `boundary.document_clause` is
    applied on this screen, so there is nothing left for this file to remember.
    This is the browse query and the stats behind it, so a vaulted document
    reaching here is listed by title *and* counted — and a hidden row that
    still moves a total says "there is something here you cannot see", which is
    more than nothing.
    """
    return (
        sa.select(
            Document,
            SourceFile.original_filename,
            SourceFile.ingest_source,
            SourceFile.received_at,
            SourceFile.page_count.label("file_page_count"),
            SourceFile.sha256,
            Correspondent.name.label("correspondent"),
            DocumentType.name.label("document_type"),
            KnownForm.code.label("known_form"),
        )
        .join(SourceFile, SourceFile.id == Document.source_file_id)
        .outerjoin(Correspondent, Correspondent.id == Document.correspondent_id)
        .outerjoin(DocumentType, DocumentType.id == Document.document_type_id)
        .outerjoin(KnownForm, KnownForm.id == Document.known_form_id)
        .where(bound.only(Document), live())
    )


async def _tags_for(session: AsyncSession, document_ids: list[uuid.UUID]):
    if not document_ids:
        return {}
    rows = (
        await session.execute(
            sa.select(DocumentTag.document_id, Tag.id, Tag.name, DocumentTag.source)
            .join(Tag, Tag.id == DocumentTag.tag_id)
            .where(DocumentTag.document_id.in_(document_ids), live_tag_links())
            .order_by(Tag.name)
        )
    ).all()
    grouped: dict[uuid.UUID, list[TagOut]] = defaultdict(list)
    for document_id, tag_id, name, source in rows:
        grouped[document_id].append(
            TagOut(id=tag_id, name=name, source=getattr(source, "value", str(source)))
        )
    return grouped


def _entry(row, tags) -> ArchiveEntryOut:
    document = row[0]
    return ArchiveEntryOut(
        document_id=document.id,
        source_file_id=document.source_file_id,
        title=document.title,
        original_filename=row.original_filename,
        page_start=document.page_start,
        page_end=document.page_end,
        file_page_count=row.file_page_count,
        document_date=document.document_date,
        received_at=row.received_at,
        # How it got in — scanner, drag-and-drop, bulk import.
        ingest_source=getattr(row.ingest_source, "value", str(row.ingest_source)),
        correspondent=row.correspondent,
        document_type=row.document_type,
        known_form=row.known_form,
        review_state=document.review_state.value,
        sensitivity=document.sensitivity.value,
        is_backlog=document.is_backlog,
        sha256=row.sha256,
        tags=tags.get(document.id, []),
    )


@router.get("/archive", response_model=ArchiveOut)
async def browse(
    q: str | None = Query(None, description="Substring match on title or filename"),
    correspondent: list[str] = Query(default_factory=list),
    document_type: list[str] = Query(default_factory=list),
    tag: list[str] = Query(default_factory=list),
    review_state: list[ReviewState] = Query(default_factory=list),
    year: int | None = None,
    sort: str = Query("newest", pattern="^(newest|oldest|date|title)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ArchiveOut:
    """Everything in the archive, newest first, with how it arrived."""
    bound = await repository.scope_for(session, user.id)
    if not bound.visible:
        return ArchiveOut(total=0, entries=[], stats=ArchiveStatsOut())

    statement = _base(bound)
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            sa.or_(Document.title.ilike(pattern), SourceFile.original_filename.ilike(pattern))
        )
    if correspondent:
        statement = statement.where(Correspondent.name.in_(correspondent))
    if document_type:
        statement = statement.where(DocumentType.name.in_(document_type))
    if review_state:
        statement = statement.where(
            Document.review_state.in_([state.value for state in review_state])
        )
    if year:
        statement = statement.where(
            sa.extract("year", sa.func.coalesce(Document.document_date, SourceFile.received_at))
            == year
        )
    if tag:
        statement = statement.where(
            sa.exists(
                sa.select(1)
                .select_from(DocumentTag)
                .join(Tag, Tag.id == DocumentTag.tag_id)
                .where(
                    DocumentTag.document_id == Document.id,
                    DocumentTag.removed_at.is_(None),
                    Tag.name.in_(tag),
                )
            )
        )

    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(statement.subquery())
        )
    ).scalar_one()

    rows = (
        await session.execute(statement.order_by(*SORTS[sort]).limit(limit).offset(offset))
    ).all()
    tags = await _tags_for(session, [row[0].id for row in rows])

    return ArchiveOut(
        total=total,
        entries=[_entry(row, tags) for row in rows],
        stats=await _stats(session, bound),
    )


async def _stats(session: AsyncSession, bound: Scope) -> ArchiveStatsOut:
    documents = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Document)
            .where(bound.only(Document), live())
        )
    ).scalar_one()
    # The file and page totals had the library filter and no vault filter, so a
    # vaulted file was hidden from every list on this screen and still counted
    # in the header above them. `only(SourceFile)` is both halves at once.
    files = (
        await session.execute(
            sa.select(sa.func.count()).select_from(SourceFile).where(bound.only(SourceFile))
        )
    ).scalar_one()
    pages = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(SourceFile.page_count), 0))
            .select_from(SourceFile)
            .where(bound.only(SourceFile))
        )
    ).scalar_one()
    # Counted separately, because they go to different places. The header used
    # to add them together and link the total to the daily queue — which
    # excludes backlog — so it advertised 408 documents awaiting review and
    # sent you to an empty screen.
    needs_review = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Document).where(
                bound.only(Document), live(),
                Document.review_state == ReviewState.NEEDS_REVIEW.value,
                Document.is_backlog.is_(False),
            )
        )
    ).scalar_one()
    backlog_pending = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Document).where(
                bound.only(Document), live(),
                Document.review_state == ReviewState.NEEDS_REVIEW.value,
                Document.is_backlog.is_(True),
            )
        )
    ).scalar_one()
    unclassified = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Document).where(
                bound.only(Document), live(),
                Document.review_state == ReviewState.PENDING_CLASSIFICATION.value,
            )
        )
    ).scalar_one()
    return ArchiveStatsOut(
        documents=documents, files=files, pages=int(pages or 0),
        needs_review=needs_review, backlog_pending=backlog_pending,
        unclassified=unclassified,
    )


@router.get("/archive/tree", response_model=TreeOut)
async def tree(
    group_by: str = Query("year", pattern="^(year|correspondent|type|form)$"),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TreeOut:
    """The archive as a browsable hierarchy.

    `year` mirrors how Phase 6 lays the files out on disk, so the tree you click
    through and the tree you'd see in Finder are the same tree.
    """
    bound = await repository.scope_for(session, user.id)
    if not bound.visible:
        return TreeOut(group_by=group_by, groups=[])

    label = {
        "year": sa.func.to_char(
            sa.func.coalesce(Document.document_date, sa.cast(SourceFile.received_at, sa.Date)),
            "YYYY",
        ),
        "correspondent": sa.func.coalesce(Correspondent.name, "(no correspondent)"),
        "type": sa.func.coalesce(DocumentType.name, "(unclassified)"),
        "form": sa.func.coalesce(KnownForm.name, "(not a known form)"),
    }[group_by]

    rows = (
        await session.execute(
            sa.select(label.label("label"), sa.func.count().label("count"))
            .select_from(Document)
            .join(SourceFile, SourceFile.id == Document.source_file_id)
            .outerjoin(Correspondent, Correspondent.id == Document.correspondent_id)
            .outerjoin(DocumentType, DocumentType.id == Document.document_type_id)
            .outerjoin(KnownForm, KnownForm.id == Document.known_form_id)
            .where(bound.only(Document), live())
            .group_by(label)
            .order_by(label.desc() if group_by == "year" else label)
        )
    ).all()

    return TreeOut(
        group_by=group_by,
        groups=[TreeGroupOut(label=row.label, count=row.count) for row in rows],
    )


@router.post("/source-files/{source_file_id}/rescan", response_model=RescanResultOut)
async def rescan(
    source_file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> RescanResultOut:
    """Read this file again from the original, from scratch.

    Requeues `normalize`, which re-runs OCR and cascades through paging,
    segmentation, embedding and classification — so it is the honest meaning of
    "rescan" rather than a partial repair.

    It is safe to press at any time. The original is never touched; everything
    this rebuilds is derived. And it is the answer to the one situation the
    pipeline could not previously get itself out of: a page ocrmypdf declined to
    OCR, which produces a document that is fully processed and completely
    unsearchable, with no failure anywhere to retry.
    """
    library_ids = await repository.writable_library_ids(session, user.id)
    source_file = await session.get(SourceFile, source_file_id)
    # 404 rather than 403 for a file in a library the caller cannot write:
    # a 403 confirms it exists.
    if source_file is None or source_file.library_id not in library_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    queued = await queue.requeue_stage(
        session, JobStage.NORMALIZE, source_file_id=source_file.id
    )
    await record(
        session,
        entity_type="source_file",
        entity_id=source_file.id,
        action="rescan_requested",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"queued": queued},
    )
    await events.publish(
        session,
        [events.Topic.FILES, events.Topic.JOBS],
        library_id=source_file.library_id,
        source_file_id=source_file.id,
    )
    await session.commit()
    return RescanResultOut(
        source_file_id=source_file.id,
        queued=queued,
        detail=(
            "Reading it again from the original. Text, pages and filing will be "
            "rebuilt; the original file is untouched."
            if queued
            else "A rescan is already running for this file."
        ),
    )
