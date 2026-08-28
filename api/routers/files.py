"""Serving the archive's bytes — authenticated and library-scoped (REQ-106).

There are no static routes. Every render, thumbnail, word-box file and PDF is
served through a handler that resolves the caller's visible libraries first, so
a leaked URL is worth nothing without a session.
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.artifacts import derived_for, resolve_in_data
from api.auth.dependencies import current_user
from api.db import repository
from api.db.models import AppUser
from api.db.session import get_session
from api.schemas import (
    OcrPageTextOut,
    OcrTextOut,
    PageOut,
    SourceFileDetailOut,
    SourceFileOut,
)
from api.storage.blobs import blob_path

router = APIRouter(prefix="/files", tags=["files"])

# Content-addressed, so a given URL's bytes can never change. `private` because
# the response is scoped to one caller and must not be shared by a proxy.
IMMUTABLE = "private, max-age=31536000, immutable"

_NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "not found")


@router.get("/{source_file_id}", response_model=SourceFileDetailOut)
async def get_file(
    source_file_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SourceFileDetailOut:
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise _NOT_FOUND
    pages = await repository.list_pages(session, user.id, source_file_id)
    return SourceFileDetailOut(
        source_file=SourceFileOut.model_validate(source_file),
        pages=[PageOut.model_validate(page) for page in pages],
    )


@router.get("/{source_file_id}/pages/{page_number}/render")
async def page_render(
    source_file_id: uuid.UUID,
    page_number: int,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    page = await repository.get_page(session, user.id, source_file_id, page_number)
    if page is None or not page.render_path:
        raise _NOT_FOUND
    path = resolve_in_data(page.render_path)
    if not path.is_file():
        # The row says the render exists and it does not. Say so plainly rather
        # than returning a 404 that reads like "no such page".
        raise HTTPException(status.HTTP_410_GONE, "render is missing; re-run the page stage")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": IMMUTABLE})


@router.get("/{source_file_id}/pages/{page_number}/thumb")
async def page_thumb(
    source_file_id: uuid.UUID,
    page_number: int,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    page = await repository.get_page(session, user.id, source_file_id, page_number)
    if page is None or not page.thumb_path:
        raise _NOT_FOUND
    path = resolve_in_data(page.thumb_path)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "thumbnail is missing")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": IMMUTABLE})


@router.get("/{source_file_id}/pages/{page_number}/boxes")
async def page_boxes(
    source_file_id: uuid.UUID,
    page_number: int,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Word rectangles for one page — the highlight overlay's raw material.

    Only the requested page is returned: ocr.json for a 300-page bundle is large,
    and the viewer only ever draws one page at a time.
    """
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise _NOT_FOUND

    boxes_file = derived_for(source_file.sha256).word_boxes
    if not boxes_file.is_file():
        raise HTTPException(status.HTTP_410_GONE, "word boxes are missing")

    document = json.loads(boxes_file.read_text())
    for page in document.get("pages", []):
        if page.get("number") == page_number:
            return JSONResponse(page, headers={"Cache-Control": IMMUTABLE})
    raise _NOT_FOUND


@router.get("/{source_file_id}/pdf")
async def file_pdf(
    source_file_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """The searchable PDF, falling back to the untouched original.

    A file that has not been normalized yet is still worth handing over — the
    point of the archive is that the bytes are always retrievable.
    """
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise _NOT_FOUND

    normalized = derived_for(source_file.sha256).normalized_pdf
    path = normalized if normalized.is_file() else blob_path(source_file.sha256)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "the stored blob is missing — integrity alert")

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=source_file.original_filename or f"{source_file.sha256[:12]}.pdf",
        headers={"Cache-Control": IMMUTABLE},
    )


@router.get("/{source_file_id}/text", response_model=OcrTextOut)
async def ocr_text(
    source_file_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OcrTextOut:
    """Exactly what OCR read, page by page, verbatim.

    Not a summary and not cleaned up. The point is to be able to compare what
    the machine read against what is actually on the page — which matters most
    where OCR is least reliable: handwriting, carbon copies, faint fax paper. A
    search that finds nothing is ambiguous until you can see whether the word
    you searched for was ever read correctly in the first place.

    It also makes a zero-character page legible as the specific failure it is,
    rather than as a document that mysteriously will not turn up.
    """
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise _NOT_FOUND

    pages = await repository.list_pages(session, user.id, source_file_id)
    entries = [
        OcrPageTextOut(
            page_number=page.page_number,
            text=page.text or "",
            characters=len((page.text or "").strip()),
        )
        for page in pages
    ]
    return OcrTextOut(
        source_file_id=source_file.id,
        original_filename=source_file.original_filename,
        pages=entries,
        characters=sum(entry.characters for entry in entries),
        empty_pages=sum(1 for entry in entries if entry.characters == 0),
    )
