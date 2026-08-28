"""Review queue, why-panel, and undo (T-3.10 to T-3.12).

Everything the trust surface needs to be read from stored provenance rather than
reconstructed: a document's classification history, the evidence behind each
field, and one call to put any automated decision back.
"""

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import undo as undo_module
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType, ReviewState, TagSource
from api.db.models import (
    AppUser,
    Classification,
    Document,
    DocumentTag,
    FieldProvenance,
    Tag,
    live_tag_links,
)
from api.db.session import get_session
from api.schemas import (
    ClassificationOut,
    DocumentOut,
    FieldProvenanceOut,
    ReviewQueueOut,
    TagOut,
    WhyPanelOut,
)
from api.segments import live

router = APIRouter(tags=["review"])


@router.get("/review", response_model=ReviewQueueOut)
async def review_queue(
    limit: int = Query(25, ge=1, le=100),
    include_backlog: bool = Query(
        False,
        description="Backlog documents are kept out of the daily queue by default (R-03).",
    ),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReviewQueueOut:
    """What the gate declined to file unattended (REQ-059)."""
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return ReviewQueueOut(total=0, documents=[])

    conditions = [
        Document.library_id.in_(library_ids),
        live(),
        Document.review_state == ReviewState.NEEDS_REVIEW.value,
    ]
    if not include_backlog:
        conditions.append(Document.is_backlog.is_(False))

    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Document).where(sa.and_(*conditions))
        )
    ).scalar_one()
    documents = (
        await session.execute(
            sa.select(Document)
            .where(sa.and_(*conditions))
            .order_by(Document.created_at)
            .limit(limit)
        )
    ).scalars().all()

    return ReviewQueueOut(
        total=total,
        documents=[DocumentOut.model_validate(document) for document in documents],
    )


@router.get("/documents/{document_id}/why", response_model=WhyPanelOut)
async def why(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> WhyPanelOut:
    """Why every AI-written field says what it says (REQ-063).

    A direct read of stored provenance — nothing is reconstructed, because a
    reconstructed justification is not one.
    """
    document = await repository.get_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    classification = (
        await session.execute(
            sa.select(Classification)
            .where(Classification.document_id == document_id)
            .order_by(Classification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    provenance = []
    if classification is not None:
        provenance = (
            await session.execute(
                sa.select(FieldProvenance)
                .where(FieldProvenance.classification_id == classification.id)
                .order_by(FieldProvenance.field_name)
            )
        ).scalars().all()

    tags = (
        await session.execute(
            sa.select(Tag.id, Tag.name, DocumentTag.source)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == document_id, live_tag_links())
            .order_by(Tag.name)
        )
    ).all()

    return WhyPanelOut(
        document=DocumentOut.model_validate(document),
        classification=(
            ClassificationOut.model_validate(classification) if classification else None
        ),
        provenance=[FieldProvenanceOut.model_validate(row) for row in provenance],
        tags=[
            TagOut(id=tag_id, name=name, source=TagSource(source).value)
            for tag_id, name, source in tags
        ],
    )


@router.post("/documents/{document_id}/undo", response_model=DocumentOut)
async def undo(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    """Put the last automated decision back (REQ-067)."""
    document = await repository.get_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if not await repository.can_write_library(session, user.id, document.library_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")

    event = await undo_module.latest_undoable(session, document_id)
    if event is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "there is nothing to undo")

    try:
        restored = await undo_module.undo_event(session, event, actor_id=user.id)
    except undo_module.UndoError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await session.commit()
    await session.refresh(restored)
    return DocumentOut.model_validate(restored)


@router.post("/documents/{document_id}/accept", response_model=DocumentOut)
async def accept(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    """A human takes responsibility for the classification as it stands."""
    from api.audit import record

    document = await repository.get_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if not await repository.can_write_library(session, user.id, document.library_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")

    before = document.review_state.value
    document.review_state = ReviewState.FILED
    await record(
        session,
        entity_type="document",
        entity_id=document_id,
        action="file",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        before={"review_state": before},
        after={"review_state": ReviewState.FILED.value},
    )
    await session.commit()
    await session.refresh(document)
    return DocumentOut.model_validate(document)
