"""Export, integrity, mirror, backup and the audit log (Phase 6).

Everything here answers one question: *can I get my documents back?* The
endpoints are deliberately boring and synchronous where they can be — a person
clicks "Export" and gets a directory they can copy to a USB stick.

The long-running ones (export, integrity, backup) run in a thread rather than
blocking the event loop, and each records an audit event, because "I exported
the whole archive" is exactly the kind of thing you want a record of.
"""

import asyncio
import logging
import uuid
from datetime import date, datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import ai_ask, ask, health_panel, offsite, offsite_runs, settings_store
from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType, Sensitivity
from api.db.models import AppUser, AuditEvent, Document, Library, Rule
from api.db.scope import resolve
from api.db.session import get_session
from api.export import archive_export, backup, integrity, mirror
from api.routers.settings import admin_only
from api.schemas import (
    AskIn,
    AskOut,
    AuditEventOut,
    AuditPageOut,
    BackupOut,
    DocumentOut,
    ExportOut,
    ExportRequestIn,
    FileTreeNodeOut,
    FileTreeOut,
    GoBagIn,
    HealthPanelOut,
    IntegrityOut,
    MirrorOut,
    OffsiteRunOut,
    OffsiteStatusOut,
)
from api.segments import live
from api.vault import boundary as vault_boundary

log = logging.getLogger("bindery.trust")

router = APIRouter(tags=["trust"])


async def _visible(session: AsyncSession, user: AppUser) -> list[uuid.UUID]:
    ids = await repository.visible_library_ids(session, user.id)
    if not ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no visible libraries")
    return ids


def _as_out(result: archive_export.ExportResult) -> ExportOut:
    return ExportOut(
        path=str(result.path),
        document_count=result.document_count,
        file_count=result.file_count,
        byte_size=result.byte_size,
        encrypted=result.encrypted,
        missing_blobs=result.missing_blobs,
    )


# --------------------------------------------------------------------------
# Vital records (T-6.1, REQ-091)
# --------------------------------------------------------------------------


@router.get("/vital", response_model=list[DocumentOut])
async def vital_records(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> list[Document]:
    """The documents you would need in a hurry, without searching for them.

    Birth certificate, DD-214, deed, passport. On the home screen because the
    day you need them is not a day you want to be composing a query.
    """
    library_ids = await _visible(session, user)
    return list(
        (
            await session.execute(
                sa.select(Document)
                .where(
                    Document.library_id.in_(library_ids),
                    Document.sensitivity == Sensitivity.VITAL,
                    live(),
                    # The home screen is a read path like any other, and this
                    # one had no vault clause (CR-007). A vital record is the
                    # likeliest thing anyone vaults — ADR-012 names the deed
                    # and the discharge papers itself — and `seal` never
                    # touches `sensitivity`, so a sealed VITAL document kept
                    # matching here whether the vault was open or shut.
                    vault_boundary.document_clause(user.id),
                )
                .order_by(Document.document_date.desc().nullslast(), Document.title)
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------
# Export (T-6.2, T-6.3)
# --------------------------------------------------------------------------


@router.post("/export/full", response_model=ExportOut)
async def export_full(
    body: ExportRequestIn,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> ExportOut:
    library_ids = await _visible(session, user)
    result = await archive_export.full_export(session, library_ids, name=body.name)
    await record(
        session,
        entity_type="export",
        entity_id=uuid.uuid4(),
        action="export_full",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={
            "path": str(result.path),
            "documents": result.document_count,
            "files": result.file_count,
            "missing_blobs": result.missing_blobs,
        },
    )
    await session.commit()
    return _as_out(result)


@router.post("/export/go-bag", response_model=ExportOut)
async def export_go_bag(
    body: GoBagIn,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> ExportOut:
    """The vital tier, encrypted, small enough to carry (REQ-092)."""
    library_ids = await _visible(session, user)
    try:
        result = await archive_export.go_bag(session, library_ids, body.passphrase)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error

    await record(
        session,
        entity_type="export",
        entity_id=uuid.uuid4(),
        action="export_go_bag",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        # The passphrase is not here, and must never be.
        after={
            "path": str(result.path),
            "documents": result.document_count,
            "encrypted": True,
        },
    )
    await session.commit()
    return _as_out(result)


# --------------------------------------------------------------------------
# Integrity, mirror, backup (T-6.4, T-6.5, T-6.6)
# --------------------------------------------------------------------------


@router.post("/integrity/check", response_model=IntegrityOut)
async def integrity_check(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> IntegrityOut:
    """Re-hash every original. Run this before a backup, not after (REQ-095)."""
    await _visible(session, user)
    report = await integrity.check(session)
    await record(
        session,
        entity_type="integrity",
        entity_id=uuid.uuid4(),
        action="integrity_check",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={
            "checked": report.checked,
            "healthy": report.healthy,
            "corrupt": len(report.corrupt),
            "missing": len(report.missing),
        },
    )
    await session.commit()
    return IntegrityOut(**report.as_dict())


@router.post("/mirror/rebuild", response_model=MirrorOut)
async def mirror_rebuild(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> MirrorOut:
    """Regenerate the browsable folder tree. Safe to run any time (REQ-094)."""
    library_ids = await _visible(session, user)
    result = await mirror.rebuild(session, library_ids)
    return MirrorOut(
        root=str(result.root),
        linked=result.linked,
        copied=result.copied,
        missing=result.missing,
        bundles=result.bundles,
        removed=result.removed,
    )


@router.post("/backup/run", response_model=BackupOut)
async def backup_run(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
    force: bool = Query(False, description="back up even if integrity is failing"),
) -> BackupOut:
    """Integrity check, then dump, then blobs — in that order (REQ-096)."""
    await _visible(session, user)
    report = await integrity.check(session)
    try:
        # pg_dump and a large file copy are both blocking; keep the event loop free.
        result = await asyncio.to_thread(
            backup.run_backup, report, destination=None, allow_unhealthy=force
        )
    except RuntimeError as error:
        # The integrity refusal. A 409 rather than a 500: the request is valid,
        # the archive's state is not.
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    await record(
        session,
        entity_type="backup",
        entity_id=uuid.uuid4(),
        action="backup_run",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={
            "path": str(result.path),
            "blobs": result.blob_count,
            "integrity_healthy": result.integrity_healthy,
            "forced": force,
        },
    )
    await session.commit()
    return BackupOut(
        path=str(result.path),
        blob_count=result.blob_count,
        byte_size=result.byte_size,
        integrity_healthy=result.integrity_healthy,
        manifest=result.manifest,
    )



# --------------------------------------------------------------------------
# Offsite replication (T-13.7, REQ-164, ADR-010)
# --------------------------------------------------------------------------


@router.get("/offsite", response_model=OffsiteStatusOut)
async def offsite_status(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> OffsiteStatusOut:
    """Whether a copy has actually left the building, and when.

    The **age** of the last success rather than a tick. A tick is a claim that
    stops being checked; "last succeeded 3 days ago" is a fact somebody can act
    on. And with nothing ever succeeded the answer is stale, not new — an empty
    history is the most alarming state this can be in, not the most neutral.
    """
    await _visible(session, user)
    config = await offsite.config_from_settings(session)
    state = await offsite_runs.status(session)
    return OffsiteStatusOut(
        configured=config.complete,
        runs=[OffsiteRunOut.model_validate(run) for run in await offsite_runs.recent(session)],
        **state,
    )


@router.post("/offsite/replicate", response_model=OffsiteStatusOut)
async def offsite_replicate_now(
    kind: str = Query("daily", pattern="^(daily|weekly)$"),
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> OffsiteStatusOut:
    """Ask for a run. The worker does it; this only records the request.

    Deliberately not a synchronous upload. A few hundred megabytes inside a
    request handler holds a connection open for minutes and dies with the
    request — and a half-finished replication is exactly the state the ordering
    rules in `offsite.replicate` exist to avoid.

    Administrator-only, for the same reason the credentials are (CR-006): a run
    is a whole-host operation — a pg_dump of every library plus every blob and
    every vault object — and `_visible` is satisfied by belonging to any
    library, which is not a statement about the host. Reading the *status* stays
    open to everyone, because "has a copy left the building?" is a question
    every member is entitled to ask about their own documents.
    """
    await _visible(session, user)
    admin_only(user)
    config = await offsite.config_from_settings(session)
    if not config.complete:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Offsite replication is not configured — missing the "
            + ", ".join(config.missing()) + ".",
        )

    run = await offsite_runs.request(session, offsite.Kind(kind), requested_by=user.id)
    if run is None:
        # 409 rather than an error: the request is valid, the state is not.
        # Two presses must not become two uploads — with versioning on and no
        # delete permission, a duplicate object version is permanent.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A replication run is already queued or in progress."
        )

    await record(
        session,
        entity_type="offsite_run",
        entity_id=run.id,
        action="offsite_replicate_requested",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"kind": kind},
    )
    await session.commit()
    return await offsite_status(session=session, user=user)

# --------------------------------------------------------------------------
# Audit log viewer (T-6.8, REQ-069)
# --------------------------------------------------------------------------


@router.get("/audit", response_model=AuditPageOut)
async def audit_log(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
    entity_id: uuid.UUID | None = Query(None, description="one document's history"),
    entity_type: str | None = None,
    actor_type: str | None = Query(None, description="human, ai, rule or system"),
    action: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_sequence: int | None = Query(None, description="keyset cursor"),
    limit: int = Query(100, ge=1, le=500),
) -> AuditPageOut:
    """Every mutation, filterable, newest first.

    Paged by `sequence` rather than by offset: the table only grows, and a
    keyset cursor cannot skip or repeat a row while you are reading it.
    Ordered by `sequence` rather than `created_at` because `now()` is
    transaction-scoped — events written together share a timestamp.
    """
    scope = await resolve(session, user.id)
    scope.require_any()

    conditions: list[sa.ColumnElement[bool]] = []
    if entity_id is not None:
        conditions.append(AuditEvent.entity_id == entity_id)
    if entity_type:
        conditions.append(AuditEvent.entity_type == entity_type)
    if actor_type:
        conditions.append(AuditEvent.actor_type == actor_type)
    if action:
        conditions.append(AuditEvent.action == action)
    if since is not None:
        conditions.append(AuditEvent.created_at >= since)
    if until is not None:
        conditions.append(AuditEvent.created_at <= until)
    if before_sequence is not None:
        conditions.append(AuditEvent.sequence < before_sequence)

    # The library filter comes from the scope, not from this function. An
    # audit event's `after` blob is document content by another name, and this
    # endpoint leaked every household member's history until the leak suite
    # said so.
    reachable = scope.audit().subquery()
    rows = (
        await session.execute(
            sa.select(AuditEvent, AppUser.email, Rule.name.label("rule_name"))
            .join(reachable, reachable.c.id == AuditEvent.id)
            .outerjoin(AppUser, AppUser.id == AuditEvent.actor_id)
            .outerjoin(Rule, Rule.id == AuditEvent.rule_id)
            .where(sa.and_(*conditions) if conditions else sa.true())
            .order_by(AuditEvent.sequence.desc())
            .limit(limit + 1)
        )
    ).all()

    has_more = len(rows) > limit
    events = []
    for row in rows[:limit]:
        event = row[0]
        events.append(
            AuditEventOut(
                id=event.id,
                sequence=event.sequence,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                action=event.action,
                actor_type=str(event.actor_type),
                # "Who did this" should read as a name, not a UUID.
                actor_label=row.email or row.rule_name,
                rule_id=event.rule_id,
                before=event.before,
                after=event.after,
                created_at=event.created_at,
            )
        )

    return AuditPageOut(
        events=events,
        next_before_sequence=events[-1].sequence if has_more and events else None,
    )


# --------------------------------------------------------------------------
# Browsing the tree in the app (T-6.4)
# --------------------------------------------------------------------------


@router.get("/tree", response_model=FileTreeOut)
async def browse_tree(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
    path: str = Query("", description="folder to list; empty for the top"),
) -> FileTreeOut:
    """List one level of the same tree the mirror and the export write to disk.

    Answering "what is actually in here, how did it get in, and what is it
    tagged with" should not require searching for something you already have —
    or SSHing into the box to look at a folder. This is that folder, in the app,
    computed from the database rather than read off disk, so it is correct even
    when the mirror has not been rebuilt yet.
    """
    library_ids = await _visible(session, user)
    entries = await archive_export.collect(session, library_ids)
    archive_export.plan_layout(entries)
    library_names = dict(
        (
            await session.execute(
                sa.select(Library.id, Library.name).where(Library.id.in_(library_ids))
            )
        ).all()
    )

    prefix = path.strip("/")
    depth = len(prefix.split("/")) if prefix else 0

    folders: dict[str, int] = {}
    nodes: list[FileTreeNodeOut] = []
    seen_bundles: set[str] = set()

    for entry in entries:
        relative = entry.relative_path or ""
        if prefix and not relative.startswith(f"{prefix}/"):
            continue
        parts = relative.split("/")
        if len(parts) > depth + 1:
            # Something deeper: surface the folder that contains it, once.
            folders[parts[depth]] = folders.get(parts[depth], 0) + 1
            continue

        document = entry.document
        is_bundle = "bundle" in document
        if is_bundle:
            if relative in seen_bundles:
                # Every document in a bundle shares one file; list it once and
                # let the row carry the page ranges.
                continue
            seen_bundles.add(relative)

        nodes.append(
            FileTreeNodeOut(
                path=relative,
                name=parts[-1],
                kind="bundle" if is_bundle else "document",
                document_id=uuid.UUID(document["document_id"]),
                source_file_id=uuid.UUID(document["source_file_id"]),
                title=document["title"],
                document_date=document["document_date"],
                correspondent=document["correspondent"],
                document_type=document["document_type"] or document["known_form_name"],
                tags=document["tags"],
                page_start=document["page_start"],
                page_end=document["page_end"],
                page_count=entry.page_count,
                original_filename=entry.original_filename,
                received_at=entry.received_at,
                sensitivity=document["sensitivity"],
                review_state=document["review_state"],
                ingest_source=document["ingest_source"],
                byte_size=document["byte_size"],
                library_id=uuid.UUID(document["library_id"]),
                library_name=library_names.get(uuid.UUID(document["library_id"])),
            )
        )

    folder_nodes = [
        FileTreeNodeOut(
            path=f"{prefix}/{name}" if prefix else name,
            name=name,
            kind="folder",
            child_count=count,
        )
        for name, count in sorted(folders.items())
    ]

    return FileTreeOut(
        root=prefix,
        # Folders first, then documents by date — the order a person scanning a
        # directory listing expects.
        nodes=folder_nodes
        + sorted(nodes, key=lambda n: (n.document_date or date.max, n.name)),
    )


# --------------------------------------------------------------------------
# Health panel (T-8.2, REQ-109)
# --------------------------------------------------------------------------


@router.get("/health/panel", response_model=HealthPanelOut)
async def health_panel_view(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> HealthPanelOut:
    """Queue depth, failures, stalls and spend, for what this caller can reach.

    Authenticated, unlike `/api/health`: queue contents and API spend are not
    facts to hand an unauthenticated caller. The unauthenticated endpoint stays
    a bare liveness probe for the container healthcheck.

    The scope is passed rather than computed and discarded. This route called
    `_visible` and then ignored it, which is the same shape of mistake the
    Phase 6 audit endpoint made — and `collect` accepted a `library_ids`
    argument it never used, so even a caller that passed one got the whole
    archive. The effect was a sidebar badge that could be lit by work in a
    library the viewer cannot open: a warning nobody is able to answer.

    No administrator branch, deliberately (ADR-009). An admin sees their own
    libraries here like everyone else. The archive is not left unwatched by
    that: the worker's health monitor and the webhook notifier call `collect`
    with no scope at all, so a stall anywhere is still noticed by the thing
    whose job is noticing.
    """
    panel = await health_panel.collect(session, await _visible(session, user))
    return HealthPanelOut(**panel.as_dict())


# --------------------------------------------------------------------------
# Ask (T-8.1, REQ-116)
# --------------------------------------------------------------------------


@router.post("/ask", response_model=AskOut)
async def ask_the_archive(
    body: AskIn,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> AskOut:
    """Answer a question from the archive, citing every claim to a page.

    Retrieval is Postgres full-text search, so this endpoint still does
    something useful with no API key and no network: it returns the pages that
    mention the question. That is invariant 7 — retrieval never depends on the
    Claude API — showing up as a product behaviour rather than a principle.
    """
    library_ids = await _visible(session, user)

    key = await settings_store.get(session, settings_store.ANTHROPIC_API_KEY)
    model = await settings_store.get(session, settings_store.BINDERY_MODEL) or "claude-opus-5"
    answerer = ai_ask.ClaudeAnswerer(key or "", model=model) if key else None

    # `user.id` so the vault boundary applies: Ask reads page text and sends it
    # to a model, so a vaulted document reaching it would be quoted back *and*
    # transmitted off the host.
    result = await ask.ask(
        session, body.question.strip(), library_ids, answerer, viewer=user.id
    )
    return AskOut(**result.as_dict())
