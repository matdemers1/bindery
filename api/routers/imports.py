"""Backlog import endpoints (T-4.1 to T-4.6).

Staged deliberately: scan → review the dry run → sample → curate → import.
Nothing is processed before a human has seen counts and a projected cost, and
the import is resumable at every stage.
"""

import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import bulk as bulk_module
from api.audit import record
from api.auth.dependencies import current_user
from api.config import get_settings
from api.db import repository
from api.db.enums import ActorType, ImportItemState, ImportState
from api.db.models import (
    AppUser,
    AuditEvent,
    Document,
    EventLog,
    ImportItem,
    ImportSession,
)
from api.db.session import get_session
from api.schemas import (
    BulkEditIn,
    BulkResultOut,
    ImportItemOut,
    ImportLogLineOut,
    ImportPresetsOut,
    ImportSessionOut,
    ImportStartIn,
)
from api.vault import boundary as vault
from api.vault import sweep as vault_sweep
from api.vault.session import sessions as vault_sessions

router = APIRouter(tags=["import"])

# Subtrees of DATA_ROOT that are the application's, not the operator's. Nothing
# may be imported *from* them: they hold every household's originals, OCR'd
# derivatives, mirrors, exports and sealed vault objects, and the walker admits
# exactly the shapes they are written in (`normalized.pdf`, `pages/0001.webp`).
# An import reading one of them would copy another library's documents into the
# caller's own, where they would then be legitimately searchable — invariant 4,
# ADR-005 and ADR-009 all in one request.
RESERVED_SUBTREES = ("blobs", "tmp", "derived", "vault", "mirror", "exports", "backups")


def _import_root(raw: str) -> Path:
    """The one place a requested import root is turned into a path we will read.

    The feature is deliberately "point it at a folder the worker can see", so
    this is containment rather than a fixed list of folders: the inbox, or
    anything else the operator has put under the DATA_ROOT mount. Resolved
    first, because `..` and a symlink are how a path that looks contained stops
    being contained.
    """
    settings = get_settings()
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "the path must be absolute"
        )
    candidate = candidate.resolve()

    inbox = settings.inbox_root.resolve()
    data = settings.data_root.resolve()
    refused = HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        f"imports may only read from {inbox} or another folder under {data}",
    )

    # Refused before anything is allowed, so an INBOX_ROOT pointed somewhere
    # careless cannot open them. The mount itself is refused for the same
    # reason: a walk from there descends into every one of them.
    if candidate == data or any(
        candidate.is_relative_to(data / name) for name in RESERVED_SUBTREES
    ):
        raise refused
    if candidate.is_relative_to(inbox) or candidate.is_relative_to(data):
        return candidate
    raise refused


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


async def _out(
    session: AsyncSession, import_session: ImportSession, progress: dict[str, int]
) -> ImportSessionOut:
    vaulted = awaiting = 0
    if import_session.to_vault:
        vaulted = await vault_sweep.vaulted(session, import_session)
        awaiting = await vault_sweep.awaiting(session, import_session)
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
        to_vault=import_session.to_vault,
        vaulted=vaulted,
        awaiting_vault=awaiting,
        # `peek`, not `is_unlocked`: the Import screen polls this route every
        # five seconds while a vault-bound import is still sealing, so a
        # touching read here meant an open tab held the vault unlocked
        # indefinitely — ADR-012's fifteen minutes became "until the browser
        # closes", for exactly the screen most likely to be left open.
        # Reporting that the vault is open is not using it.
        vault_unlocked=bool(
            import_session.created_by
            and vault_sessions.peek(import_session.created_by) is not None
        ),
    )


@router.get("/imports", response_model=list[ImportSessionOut])
async def list_imports(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    from api.backlog.session import progress as progress_of

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
    return [await _out(session, row, await progress_of(session, row)) for row in rows]


@router.post("/imports", response_model=ImportSessionOut, status_code=status.HTTP_201_CREATED)
async def start_import(
    payload: ImportStartIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    """Scan a directory and produce a dry run. **Ingests nothing.**"""
    from api.backlog.session import progress as progress_of
    from api.backlog.session import scan

    if not await repository.can_write_library(session, user.id, payload.library_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")

    root = _import_root(payload.root_path)

    if payload.to_vault and not vault_sessions.is_unlocked(user.id):
        # Refused now rather than accepted and never honoured: an import bound
        # for a vault nobody can open would sit in the archive indefinitely
        # with a box ticked that says otherwise.
        raise HTTPException(
            status.HTTP_423_LOCKED,
            "unlock your vault first — an import bound for the vault is sealed as "
            "its files finish, and that needs the vault open",
        )

    import_session = ImportSession(
        library_id=payload.library_id, root_path=str(root),
        sample_size=payload.sample_size, created_by=user.id,
        to_vault=payload.to_vault,
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
    return await _out(session, import_session, await progress_of(session, import_session))


@router.get("/imports/presets", response_model=ImportPresetsOut)
async def presets(user: AppUser = Depends(current_user)) -> ImportPresetsOut:
    """Paths the screen offers without anyone typing them (REQ-196). The inbox
    is the watched folder every other feature already knows about."""
    return ImportPresetsOut(inbox=str(get_settings().inbox_root))


@router.get("/imports/{session_id}/log", response_model=list[ImportLogLineOut])
async def import_log(
    session_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=1000),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    """What the pipeline said about this import's files (REQ-196).

    The per-file outcomes live on `import_item`; this is the other half — the
    worker's own log lines for those files, so "failed" comes with the why
    that was written at the time rather than reconstructed later.
    """
    await _owned(session, user, session_id)
    file_ids = sa.select(ImportItem.source_file_id).where(
        ImportItem.session_id == session_id, ImportItem.source_file_id.is_not(None)
    )
    rows = (
        await session.execute(
            sa.select(EventLog)
            .where(EventLog.source_file_id.in_(file_ids))
            .order_by(EventLog.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        ImportLogLineOut(
            at=row.created_at, level=row.level, message=row.message,
            source_file_id=row.source_file_id, stage=row.stage,
        )
        for row in rows
    ]


@router.get("/imports/{session_id}", response_model=ImportSessionOut)
async def get_import(
    session_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    from api.backlog.session import progress as progress_of

    import_session = await _owned(session, user, session_id)
    return await _out(session, import_session, await progress_of(session, import_session))


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
    from api.backlog.session import progress as progress_of
    from api.backlog.session import select_sample

    import_session = await _owned(session, user, session_id)
    await select_sample(session, import_session)
    await session.commit()
    return await _out(session, import_session, await progress_of(session, import_session))


@router.patch("/imports/{session_id}", response_model=ImportSessionOut)
async def set_import_options(
    session_id: uuid.UUID,
    to_vault: bool = Body(..., embed=True),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    """Bind an import to the vault — or unbind it — at any point (REQ-197).

    The first version took the flag at *scan* time only. A person who scanned,
    read what was found, and then ticked "straight into the vault" before
    pressing Import had their choice dropped in silence: 309 files went into
    the ordinary archive with the box ticked. The flag belongs to the import,
    not the scan, and it must be settable afterwards too — binding a completed
    import is how those 309 get where they were meant to go.
    """
    from api.backlog.session import progress as progress_of

    import_session = await _owned(session, user, session_id)
    if to_vault and not vault_sessions.is_unlocked(user.id):
        raise HTTPException(
            status.HTTP_423_LOCKED,
            "unlock your vault first — files are sealed as they finish, and that "
            "needs the vault open",
        )
    before = import_session.to_vault
    import_session.to_vault = to_vault
    await record(
        session, entity_type="import_session", entity_id=import_session.id,
        action="import_to_vault" if to_vault else "import_not_to_vault",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        before={"to_vault": before}, after={"to_vault": to_vault},
    )
    await session.commit()
    return await _out(session, import_session, await progress_of(session, import_session))


@router.post("/imports/{session_id}/run", response_model=ImportSessionOut)
async def run(
    session_id: uuid.UUID,
    batch: int = Query(50, ge=1, le=500),
    to_vault: bool | None = Query(
        None, description="bind this import to the vault as part of starting it"
    ),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    """Ingest the next slice. Idempotent — calling it again is the resume.

    `to_vault` here rather than only at scan time: the choice belongs to the
    moment of importing, and the screen sends what the box says *now*.
    """
    from api.backlog.session import ingest_batch, mark_backlog
    from api.backlog.session import progress as progress_of

    import_session = await _owned(session, user, session_id)
    if to_vault is not None and to_vault != import_session.to_vault:
        if to_vault and not vault_sessions.is_unlocked(user.id):
            raise HTTPException(
                status.HTTP_423_LOCKED,
                "unlock your vault first — files are sealed as they finish, and "
                "that needs the vault open",
            )
        import_session.to_vault = to_vault
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
    return await _out(session, import_session, await progress_of(session, import_session))


@router.post("/imports/{session_id}/pause", response_model=ImportSessionOut)
async def pause(
    session_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportSessionOut:
    from api.backlog.session import progress as progress_of

    import_session = await _owned(session, user, session_id)
    import_session.state = ImportState.PAUSED
    await session.commit()
    return await _out(session, import_session, await progress_of(session, import_session))


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
    from api.backlog.session import progress as progress_of

    import_session = await _owned(session, user, session_id)
    import_session.state = ImportState.CURATING
    import_session.pass_number = 2
    await session.commit()
    return await _out(session, import_session, await progress_of(session, import_session))


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


async def _undoable(
    session: AsyncSession, user: AppUser, operation_id: uuid.UUID
) -> None:
    """Refuse an operation id that reaches outside the caller's boundary.

    `bulk.undo` resolves the operation's audit event and each document in its
    manifest by id alone, so without this the id *is* the permission: anyone
    who has ever seen one — they appear in audit rows, export manifests and
    URLs — can reverse another household's bulk edit, including after their
    membership ended. The boundary is the library, not the actor: a co-owner
    undoing what the other did is the ordinary case (invariant 4). The vault
    clause is here for the same reason it is on every other archive write — an
    undo that reaches a sealed document is a write to something the archive is
    supposed to be unable to see at all (ADR-012).
    """
    event = await session.get(AuditEvent, operation_id)
    if event is None or event.action != "bulk_edit":
        # Not ours to judge — `bulk.undo` says what is wrong with it.
        return

    document_ids = {
        uuid.UUID(entry["document_id"])
        for entry in (event.before or {}).get("manifest", [])
        if entry.get("document_id")
    }
    library_ids = await repository.writable_library_ids(session, user.id)
    mine = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Document)
            .where(
                Document.id.in_(document_ids),
                Document.library_id.in_(library_ids or [uuid.uuid4()]),
                vault.document_clause(user.id),
            )
        )
    ).scalar_one()
    if mine != len(document_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


@router.post("/bulk/{operation_id}/undo", response_model=BulkResultOut)
async def bulk_undo(
    operation_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BulkResultOut:
    """Reverse the whole operation in one action, not one document at a time."""
    await _undoable(session, user, operation_id)
    try:
        restored = await bulk_module.undo(session, operation_id, actor_id=user.id)
    except bulk_module.BulkError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    return BulkResultOut(matched=restored, operation_id=operation_id, changes=[])
