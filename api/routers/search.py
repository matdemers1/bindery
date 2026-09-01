import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import SourceFileState
from api.db.models import AppUser
from api.db.session import get_session
from api.schemas import SearchResponseOut
from api.search import query as search_query

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponseOut)
async def search(
    q: str = Query("", description="Full-text query; quoted phrases and -exclusions work"),
    library_id: list[uuid.UUID] = Query(default_factory=list),
    state: list[SourceFileState] = Query(default_factory=list),
    known_form: list[str] = Query(default_factory=list, description="Known-form codes"),
    received_from: date | None = None,
    received_to: date | None = None,
    source_file_id: uuid.UUID | None = Query(None, description="Search inside one file"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SearchResponseOut:
    """Page-anchored search across the caller's libraries.

    The visible-library set is read from the caller, never from the request, so
    a hand-written `library_id` can only ever narrow the scope.
    """
    visible = await repository.visible_library_ids(session, user.id)
    response = await search_query.search(
        session,
        q,
        visible,
        # Who is asking, so the vault filter applies. Without it search reads
        # page text straight past the boundary — the one leak that surfaces as
        # the actual words rather than a title.
        viewer=user.id,
        filters=search_query.SearchFilters(
            library_ids=library_id,
            received_from=received_from,
            received_to=received_to,
            states=state,
            known_form_codes=known_form,
            source_file_id=source_file_id,
        ),
        limit=limit,
        offset=offset,
    )
    return SearchResponseOut.model_validate(response)
