"""Backlog import endpoints (T-4.1 to T-4.6).

Staged deliberately: scan → review the dry run → sample → curate → import.
Nothing is processed before a human has seen counts and a projected cost, and
the import is resumable at every stage.
"""

import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import bulk as bulk_module
from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType, ImportItemState, ImportState
from api.db.models import AppUser, ImportItem, ImportSession
from api.db.session import get_session
from api.schemas import (
    BulkEditIn,
    BulkResultOut,
    ImportItemOut,
    ImportSessionOut,
    ImportStartIn,
)

router = APIRouter(tags=["import"])


async def _owned(session: AsyncSession, user: AppUser, session_id: uuid.UUID) -> ImportSession:
    library_ids = await repository.writable_library_ids(session, user.id)
    found = (
        await session.execute(
            sa.select(ImportSession).where(
                ImportSession.id == session_id,
                ImportSession.library_id.in_(library_ids or [uuid.uuid4()]),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return found


def _out(import_session: ImportSession, progress: dict[str, int]) -> ImportSessionOut:
    return ImportSessionOut(
        id=import_session.id,
        library_id=import_session.library_id,
        root_path=import_session.root_path,
        state=import_session.state.value,
        pass_number=import_session.pass_number,
        sample_size=import_session.sample_size,
        dry_run=import_session.dry_run,
        cost_estimate=import_session.cost_estimate,
        progress=progress,
        last_error=import_session.last_error,
        created_at=import_session.created_at,
    )


@router.get("/imports", response_model=list[ImportSessionOut])
async def list_imports(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    from worker.backlog.session import progress as progress_of

    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return []
    rows = (
        await session.execute(
            sa.select(ImportSession)
            .where(ImportSession.library_id.in_(library_ids))
            .order_by(ImportSession.created_at.desc())
        )
    ).scalars().all()
    return [_out(row, await progress_of(session, row)) for row in rows]


@router.post("/imports", response_model=ImportSessionOut, status_code=status.HTTP_201_CREATED)
async def start_import(
    payload: ImportStartIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    """Scan a directory and produce a dry run. **Ingests nothing.**"""
    from worker.backlog.session import progress as progress_of
    from worker.backlog.session import scan

    if not await repository.can_write_library(session, user.id, payload.library_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")

    root = Path(payload.root_path)
    if not root.is_absolute():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "the path must be absolute"
        )

    import_session = ImportSession(
        library_id=payload.library_id, root_path=str(root),
        sample_size=payload.sample_size, created_by=user.id,
    )
    session.add(import_session)
    await session.flush()

    await scan(session, import_session)
    await record(
        session, entity_type="import_session", entity_id=import_session.id,
        action="scan", actor_type=ActorType.HUMAN, actor_id=user.id,
        after={"root_path": str(root), "dry_run": import_session.dry_run},
    )
    await session.commit()
    await session.refresh(import_session)
    return _out(import_session, await progress_of(session, import_session))


@router.get("/imports/{session_id}", response_model=ImportSessionOut)
async def get_import(
    session_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    from worker.backlog.session import progress as progress_of

    import_session = await _owned(session, user, session_id)
    return _out(import_session, await progress_of(session, import_session))


@router.get("/imports/{session_id}/items", response_model=list[ImportItemOut])
async def list_items(
    session_id: uuid.UUID,
    state: list[ImportItemState] = Query(default_factory=list),
    limit: int = Query(100, ge=1, le=500),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    """Per-file state, including everything that failed and why (invariant 8)."""
    await _owned(session, user, session_id)
    statement = sa.select(ImportItem).where(ImportItem.session_id == session_id)
    if state:
        statement = statement.where(
            ImportItem.state.in_([value.value for value in state])
        )
    rows = (
        await session.execute(statement.order_by(ImportItem.path).limit(limit))
    ).scalars().all()
    return [
        ImportItemOut(
            path=row.path, state=row.state.value, byte_size=row.byte_size,
            sha256=row.sha256, source_file_id=row.source_file_id, error=row.error,
        )
        for row in rows
    ]


@router.post("/imports/{session_id}/sample", response_model=ImportSessionOut)
async def sample(
    session_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    """Pick pass one's stratified sample (REQ-084)."""
    from worker.backlog.session import progress as progress_of
    from worker.backlog.session import select_sample

    import_session = await _owned(session, user, session_id)
    await select_sample(session, import_session)
    await session.commit()
    return _out(import_session, await progress_of(session, import_session))


@router.post("/imports/{session_id}/run", response_model=ImportSessionOut)
async def run(
    session_id: uuid.UUID,
    batch: int = Query(50, ge=1, le=500),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    """Ingest the next slice. Idempotent — calling it again is the resume."""
    from worker.backlog.session import ingest_batch, mark_backlog
    from worker.backlog.session import progress as progress_of

    import_session = await _owned(session, user, session_id)
    if import_session.state is ImportState.PAUSED:
        import_session.state = ImportState.IMPORTING

    states = (
        [ImportItemState.SAMPLED]
        if import_session.state is ImportState.SAMPLING
        else [ImportItemState.PENDING, ImportItemState.SAMPLED]
    )
    done = await ingest_batch(session, import_session, states=states, limit=batch)
    # R-03: these never reach the daily review queue.
    await mark_backlog(session, import_session)

    remaining = (
        await session.execute(
            sa.select(sa.func.count()).select_from(ImportItem).where(
                ImportItem.session_id == session_id,
                ImportItem.state.in_(
                    [ImportItemState.PENDING.value, ImportItemState.SAMPLED.value]
                ),
            )
        )
    ).scalar_one()

    if remaining == 0:
        import_session.state = ImportState.COMPLETED
    elif done:
        import_session.state = ImportState.IMPORTING
    await session.commit()
    return _out(import_session, await progress_of(session, import_session))


@router.post("/imports/{session_id}/pause", response_model=ImportSessionOut)
async def pause(
    session_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    from worker.backlog.session import progress as progress_of

    import_session = await _owned(session, user, session_id)
    import_session.state = ImportState.PAUSED
    await session.commit()
    return _out(import_session, await progress_of(session, import_session))


@router.post("/imports/{session_id}/curate", response_model=ImportSessionOut)
async def curate(
    session_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    """Stop between passes so the taxonomy can be tidied.

    The highest-leverage human hour in the project: merging near-duplicate tags
    *before* the other several thousand documents are classified against them.
    """
    from worker.backlog.session import progress as progress_of

    import_session = await _owned(session, user, session_id)
    import_session.state = ImportState.CURATING
    import_session.pass_number = 2
    await session.commit()
    return _out(import_session, await progress_of(session, import_session))


# --------------------------------------------------------------------------
# Bulk edit (T-4.6)
# --------------------------------------------------------------------------


@router.post("/bulk/preview", response_model=BulkResultOut)
async def bulk_preview(
    payload: BulkEditIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BulkResultOut:
    """What would change. Produced by the apply path with writes off."""
    library_ids = await repository.writable_library_ids(session, user.id)
    try:
        result = await bulk_module.apply(
            session, payload.document_ids, payload.actions, library_ids,
            actor_id=user.id, dry_run=True,
        )
    except bulk_module.BulkError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await session.rollback()
    return BulkResultOut(
        matched=result.matched, operation_id=None,
        changes=[
            {"document_id": str(c.document_id), "title": c.title, "changes": c.changes}
            for c in result.changes
        ],
    )


@router.post("/bulk/apply", response_model=BulkResultOut)
async def bulk_apply(
    payload: BulkEditIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BulkResultOut:
    library_ids = await repository.writable_library_ids(session, user.id)
    try:
        result = await bulk_module.apply(
            session, payload.document_ids, payload.actions, library_ids,
            actor_id=user.id, dry_run=False,
        )
    except bulk_module.BulkError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await session.commit()
    return BulkResultOut(
        matched=result.matched, operation_id=result.operation_id,
        changes=[
            {"document_id": str(c.document_id), "title": c.title, "changes": c.changes}
            for c in result.changes
        ],
    )


@router.post("/bulk/{operation_id}/undo", response_model=BulkResultOut)
async def bulk_undo(
    operation_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BulkResultOut:
    """Reverse the whole operation in one action, not one document at a time."""
    try:
        restored = await bulk_module.undo(session, operation_id, actor_id=user.id)
    except bulk_module.BulkError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    return BulkResultOut(matched=restored, operation_id=operation_id, changes=[])
