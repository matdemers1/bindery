"""Segmentation endpoints (T-2.3, REQ-036, REQ-037, REQ-042).

Every write is audited, reversible, and leaves the source file untouched.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api import segments
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType
from api.db.models import AppUser
from api.db.session import get_session
from api.export.segment_pdf import export_segment
from api.forms.registry import match_documents
from api.schemas import DocumentOut, SegmentListOut, SegmentReplaceIn

router = APIRouter(tags=["segments"])


async def _writable_source_file(session: AsyncSession, user: AppUser, source_file_id: uuid.UUID):
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if not await repository.can_write_library(session, user.id, source_file.library_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")
    return source_file


@router.get("/files/{source_file_id}/segments", response_model=SegmentListOut)
async def list_segments(
    source_file_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SegmentListOut:
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    documents = await segments.list_segments(session, source_file_id)
    return SegmentListOut(
        source_file_id=source_file_id,
        page_count=source_file.page_count or 0,
        segments=[DocumentOut.model_validate(document) for document in documents],
    )


@router.put("/files/{source_file_id}/segments", response_model=SegmentListOut)
async def replace_segments(
    source_file_id: uuid.UUID,
    payload: SegmentReplaceIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SegmentListOut:
    """Replace a file's segments in one audited, reversible operation.

    The whole set is submitted at once rather than edited segment by segment:
    a bundle's segmentation is only ever valid as a complete cover of the file,
    so a partial edit has no meaningful intermediate state.
    """
    source_file = await _writable_source_file(session, user, source_file_id)

    try:
        documents = await segments.replace(
            session,
            source_file,
            [
                segments.SegmentSpec(
                    page_start=item.page_start, page_end=item.page_end, title=item.title
                )
                for item in payload.segments
            ],
            actor_type=ActorType.HUMAN,
            actor_id=user.id,
        )
    except segments.SegmentationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    # Boundaries moved, so previous form matches may no longer hold.
    await match_documents(session, documents)
    await session.commit()

    return SegmentListOut(
        source_file_id=source_file_id,
        page_count=source_file.page_count or 0,
        segments=[DocumentOut.model_validate(document) for document in documents],
    )


@router.post("/files/{source_file_id}/segments/undo", response_model=SegmentListOut)
async def undo_segmentation(
    source_file_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SegmentListOut:
    """Restore the previous segment set. The source file is never read."""
    source_file = await _writable_source_file(session, user, source_file_id)
    try:
        documents = await segments.undo(session, source_file, actor_id=user.id)
    except segments.SegmentationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await match_documents(session, documents)
    await session.commit()

    return SegmentListOut(
        source_file_id=source_file_id,
        page_count=source_file.page_count or 0,
        segments=[DocumentOut.model_validate(document) for document in documents],
    )


@router.get("/documents/{document_id}/pdf")
async def document_pdf(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """This document's pages as a standalone PDF (REQ-042)."""
    document = await repository.get_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    source_file = await repository.get_source_file(session, user.id, document.source_file_id)
    if source_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    try:
        path = export_segment(source_file.sha256, document.page_start, document.page_end)
    except FileNotFoundError as exc:
        raise HTTPException(
            status.HTTP_410_GONE, "the stored blob is missing — integrity alert"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    stem = (document.title or source_file.original_filename or document_id.hex)[:80]
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{stem}.pdf",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
