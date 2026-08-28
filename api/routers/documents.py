from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.dependencies import current_user
from api.db import repository
from api.db.models import AppUser
from api.db.session import get_session
from api.schemas import DocumentOut, LibraryOut, SourceFileOut

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
