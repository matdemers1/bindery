"""Serving the archive's bytes — authenticated and library-scoped (REQ-106).

There are no static routes. Every render, thumbnail, word-box file and PDF is
served through a handler that resolves the caller's visible libraries first, so
a leaked URL is worth nothing without a session.
"""

import asyncio
import json
import uuid
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.artifacts import derived_for, resolve_in_data
from api.auth.dependencies import current_user
from api.db import repository
from api.db.models import AppUser
from api.db.session import get_session
from api.schemas import (
    FileMatchesOut,
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

# One parse of a bundle's word boxes, not one per page turn.
#
# `ocr.json` holds every word rectangle of every page of a source file, and the
# overlay is fetched one page at a time — so paging through a 300-page scan read
# and `json.loads`'d the whole artifact 300 times, on the API's only event loop.
# Tens of megabytes of blocking read and parse per page turn is slow for the
# reader and a stall for everyone else on the box.
#
# Split per-page files would be better still, but they are the page stage's to
# write. This is the half that lives on the read path: parse once, keep the
# pages as the bytes they will be sent as, and serve the rest from memory.
#
# Keyed on the artifact's identity *and* its mtime and size, so a re-run of
# normalize that rewrites the file invalidates the entry rather than serving
# yesterday's boxes. Bounded in bytes rather than entries, because the entries
# differ in size by three orders of magnitude — and the bound never evicts the
# most recent entry, because the bundle large enough to break the budget is the
# bundle this exists for (see `_remember_page_boxes`).
_BOXES_CACHE_BYTES = 32 * 1024 * 1024
_boxes_cache: OrderedDict[tuple[str, int, int], dict[int, bytes]] = OrderedDict()
_boxes_cache_bytes = 0


def _load_page_boxes(path: Path) -> dict[int, bytes]:
    """Every page of one word-box artifact, pre-encoded. Blocking; call in a thread."""
    document = json.loads(path.read_text())
    pages: dict[int, bytes] = {}
    for page in document.get("pages", []):
        number = page.get("number")
        if isinstance(number, int):
            pages[number] = json.dumps(page, separators=(",", ":")).encode()
    return pages


def _remember_page_boxes(key: tuple[str, int, int], pages: dict[int, bytes]) -> None:
    global _boxes_cache_bytes

    size = sum(len(payload) for payload in pages.values())
    previous = _boxes_cache.pop(key, None)
    if previous is not None:
        _boxes_cache_bytes -= sum(len(payload) for payload in previous.values())
    _boxes_cache[key] = pages
    _boxes_cache_bytes += size
    # Evict the least recently used until the budget is met — but never the
    # entry just added. An artifact bigger than the whole budget used to be
    # served and dropped, which meant the one file this cache exists for, the
    # 300-page bundle whose boxes run to tens of megabytes, was the one file
    # that re-read and re-parsed on every page turn. The bound is therefore
    # "the budget, or one artifact, whichever is larger"; parsing that artifact
    # already puts a larger object on the heap transiently, so keeping the
    # compact encoded form raises no high-water mark that the read did not.
    while _boxes_cache_bytes > _BOXES_CACHE_BYTES and len(_boxes_cache) > 1:
        _, evicted = _boxes_cache.popitem(last=False)
        _boxes_cache_bytes -= sum(len(payload) for payload in evicted.values())


@router.get("/{source_file_id}", response_model=SourceFileDetailOut)
async def get_file(
    source_file_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SourceFileDetailOut:
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise _NOT_FOUND
    pages = await repository.list_page_summaries(session, user.id, source_file_id)
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
) -> Response:
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

    stat = boxes_file.stat()
    key = (source_file.sha256, stat.st_mtime_ns, stat.st_size)
    pages = _boxes_cache.get(key)
    if pages is None:
        # Off the event loop: the read and the parse are both blocking, and this
        # is the process serving every other request in the archive.
        pages = await asyncio.to_thread(_load_page_boxes, boxes_file)
        _remember_page_boxes(key, pages)
    else:
        _boxes_cache.move_to_end(key)

    payload = pages.get(page_number)
    if payload is None:
        raise _NOT_FOUND
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Cache-Control": IMMUTABLE},
    )


@router.get("/{source_file_id}/poster")
async def file_poster(
    source_file_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """A video's poster frame (REQ-194). 404 for anything without one."""
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise _NOT_FOUND
    poster = derived_for(source_file.sha256).poster
    if not poster.is_file():
        raise _NOT_FOUND
    return FileResponse(poster, media_type="image/webp", headers={"Cache-Control": IMMUTABLE})


@router.get("/{source_file_id}/original")
async def file_original(
    source_file_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """The stored bytes, as they arrived, with their own media type (REQ-195).

    `/pdf` hands back the searchable PDF and is the right thing for a document.
    This is for the things that are not documents — a video the browser will
    play, a photograph in its original format. `FileResponse` answers `Range`
    itself, which is what lets a `<video>` element seek.
    """
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise _NOT_FOUND
    path = blob_path(source_file.sha256)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "the stored blob is missing — integrity alert")
    return FileResponse(
        path,
        media_type=source_file.mime_type or "application/octet-stream",
        headers={"Cache-Control": IMMUTABLE, "Content-Disposition": "inline"},
    )


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


@router.get("/{source_file_id}/matches", response_model=FileMatchesOut)
async def file_matches(
    source_file_id: uuid.UUID,
    q: str = Query("", description="The same query the results page ran"),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileMatchesOut:
    """Every page of this file that matches `q`, in page order.

    Search finds the first occurrence and the viewer opens on it; this is what
    lets the reader reach the second. Without it the results page could count
    the other matches — “1 more matching page” — and nothing in the product
    could get to them, which is the differentiator stopping one step short of
    the thing nobody else does (D-03).
    """
    source_file = await repository.get_source_file(session, user.id, source_file_id)
    if source_file is None:
        raise _NOT_FOUND

    pages = await repository.matching_pages(session, user.id, source_file_id, q)
    return FileMatchesOut(query=q, pages=list(pages))


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

    pages = await repository.list_page_text(session, user.id, source_file_id)
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
