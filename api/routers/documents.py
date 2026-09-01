import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import editing, events, field_source
from api.auth.dependencies import current_user
from api.db import repository
from api.db.models import AppUser, DocumentTag, KnownForm, Tag, live_tag_links
from api.db.session import get_session
from api.schemas import (
    DocumentDetailOut,
    DocumentEditIn,
    DocumentEditOut,
    DocumentOut,
    FieldSourceOut,
    KnownFormOut,
    LibraryOut,
    PageOut,
    SourceFileOut,
    TagOut,
)

router = APIRouter(tags=["archive"])


@router.get("/libraries", response_model=list[LibraryOut])
async def list_libraries(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    return list(await repository.list_libraries(session, user.id))


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Documents the caller can see.

    Empty until segmentation lands in Phase 2 — a document is a page range, and
    nothing produces page ranges yet. The endpoint exists now because it is the
    one the Phase 0 exit demo fetches with an API token.
    """
    return list(await repository.list_documents(session, user.id, limit=limit, offset=offset))


@router.get("/source-files", response_model=list[SourceFileOut])
async def list_source_files(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Uploaded originals, scoped to the caller's libraries."""
    return list(await repository.list_source_files(session, user.id, limit=limit, offset=offset))


@router.get("/documents/{document_id}", response_model=DocumentDetailOut)
async def get_document(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DocumentDetailOut:
    """One document, with the pages of its range and the file it was cut from.

    Everything the viewer needs to present a page range as if it were a
    standalone document (ADR-001) — including the file's own page count, so page
    numbers can always be disambiguated (REQ-030).
    """
    document = await repository.get_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    source_file = await repository.get_source_file(session, user.id, document.source_file_id)
    known_form = (
        await session.get(KnownForm, document.known_form_id)
        if document.known_form_id
        else None
    )
    pages = [
        page
        for page in await repository.list_pages(session, user.id, document.source_file_id)
        if document.page_start <= page.page_number <= document.page_end
    ]

    sources = await field_source.sources_for(session, document.id)
    tags = (
        await session.execute(
            sa.select(Tag.id, Tag.name, DocumentTag.source)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == document.id, live_tag_links())
            .order_by(Tag.name)
        )
    ).all()

    return DocumentDetailOut(
        document=DocumentOut.model_validate(document),
        source_file=SourceFileOut.model_validate(source_file),
        known_form=KnownFormOut.model_validate(known_form) if known_form else None,
        pages=[PageOut.model_validate(page) for page in pages],
        field_sources=[
            FieldSourceOut(
                field_name=row.field_name,
                source=row.source.value,
                set_by=row.set_by,
                set_at=row.set_at,
                event_id=row.set_by_event_id,
            )
            for row in sources.values()
        ],
        tags=[
            TagOut(id=tag_id, name=name, source=source.value)
            for tag_id, name, source in tags
        ],
    )


@router.patch("/documents/{document_id}", response_model=DocumentEditOut)
async def edit_document(
    document_id: uuid.UUID,
    payload: DocumentEditIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DocumentEditOut:
    """Correct a document by hand (REQ-188, REQ-189, REQ-190).

    A field absent from the body is left alone; a field sent as `null` is
    cleared. Without that distinction there is no way to remove a wrong date,
    only to replace it with another wrong date.

    404 rather than 403 for a document the caller cannot see, because a 403
    confirms the thing exists and a probe should learn nothing (ADR-005). A
    vaulted document is invisible while the vault is locked and 404s here for
    the same reason, through the same repository scoping.
    """
    document = await repository.get_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if not await repository.can_write_library(session, user.id, document.library_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")

    sent = payload.model_dump(exclude_unset=True)
    created: dict[str, str] = {}

    try:
        # Creation first, so the ids it produces can be what the edit links to.
        if name := sent.pop("create_correspondent", None):
            made = await editing.create_correspondent(session, document.library_id, name)
            sent["correspondent_id"] = made.id
            created["correspondent"] = made.name
        if name := sent.pop("create_document_type", None):
            made = await editing.create_document_type(session, document.library_id, name)
            sent["document_type_id"] = made.id
            created["document_type"] = made.name

        add_ids = list(sent.pop("add_tag_ids", []))
        remove_ids = list(sent.pop("remove_tag_ids", []))
        for name in sent.pop("create_tags", []):
            made = await editing.create_tag(session, document.library_id, name)
            add_ids.append(made.id)
            created[f"tag:{made.name}"] = made.name

        result = await editing.apply(
            session, document, sent,
            actor_id=user.id, add_tags=add_ids, remove_tags=remove_ids,
        )
    except editing.EditError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error

    # The archive browser, the review badge and any open viewer are all showing
    # a value that just changed.
    await events.publish(
        session,
        [events.Topic.DOCUMENTS, events.Topic.REVIEW],
        library_id=document.library_id,
    )
    await session.commit()
    await session.refresh(document)

    return DocumentEditOut(
        document=DocumentOut.model_validate(document),
        changed=sorted(result.changed),
        tags_added=result.tags_added,
        tags_removed=result.tags_removed,
        created=created,
        event_id=result.event_id,
    )
