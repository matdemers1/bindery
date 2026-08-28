import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.dependencies import current_user
from api.db import repository
from api.db.models import AppUser, KnownForm
from api.db.session import get_session
from api.schemas import (
    DocumentDetailOut,
    DocumentOut,
    KnownFormOut,
    LibraryOut,
    PageOut,
    SourceFileOut,
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

    return DocumentDetailOut(
        document=DocumentOut.model_validate(document),
        source_file=SourceFileOut.model_validate(source_file),
        known_form=KnownFormOut.model_validate(known_form) if known_form else None,
        pages=[PageOut.model_validate(page) for page in pages],
    )
