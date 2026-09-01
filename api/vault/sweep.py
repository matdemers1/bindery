"""Sealing a vault-bound import as it finishes (T-18.11, REQ-197).

The worker cannot seal — the data key never leaves the api process while
unlocked (ADR-012) — and the api cannot seal a file the worker is still reading.
So this runs in the api, on a short interval, and asks a narrow question: **is
there a document from a vault-bound import whose pipeline has finished and whose
owner's vault is open right now?** If so, seal it. If the vault is shut, do
nothing and let the screen say "unlock to continue".

Each seal is its own transaction. A run that dies at document 40 of 300 has
sealed 40 and left 260 exactly as they were, to be picked up next tick.
"""

import asyncio
import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.db.enums import ActorType, JobState, SourceFileState
from api.db.models import Document, ImportItem, ImportSession, Job, SourceFile, Vault
from api.segments import live
from api.vault import service, store
from api.vault.session import sessions

log = logging.getLogger("bindery.vault.sweep")

INTERVAL_SECONDS = 15
IN_FLIGHT = tuple(state.value for state in (JobState.QUEUED, JobState.RUNNING, JobState.FAILED))


def _ready_documents(import_session_id: uuid.UUID):
    """Documents from this import that are finished and not yet vaulted."""
    busy = sa.select(Job.source_file_id).where(Job.state.in_(IN_FLIGHT))
    return (
        sa.select(Document)
        .join(SourceFile, SourceFile.id == Document.source_file_id)
        .join(ImportItem, ImportItem.source_file_id == SourceFile.id)
        .where(
            ImportItem.session_id == import_session_id,
            live(),
            Document.vaulted_by.is_(None),
            SourceFile.state == SourceFileState.PROCESSED,
            SourceFile.id.not_in(busy),
        )
    )


async def awaiting(session: AsyncSession, import_session: ImportSession) -> int:
    """How many are finished and waiting — for the screen, whether the vault is
    open or not. Also counts as "ready" from the sweep's point of view."""
    return (
        await session.execute(
            sa.select(sa.func.count()).select_from(
                _ready_documents(import_session.id).subquery()
            )
        )
    ).scalar_one()


async def vaulted(session: AsyncSession, import_session: ImportSession) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Document)
            .join(ImportItem, ImportItem.source_file_id == Document.source_file_id)
            .where(ImportItem.session_id == import_session.id, Document.vaulted_by.is_not(None))
        )
    ).scalar_one()


async def sweep_once(session: AsyncSession) -> int:
    """One pass over every vault-bound import whose owner is unlocked."""
    bound = (
        await session.execute(
            sa.select(ImportSession).where(
                ImportSession.to_vault.is_(True), ImportSession.created_by.is_not(None)
            )
        )
    ).scalars().all()

    sealed = 0
    for import_session in bound:
        owner = import_session.created_by
        if not sessions.is_unlocked(owner):
            continue
        vault = (
            await session.execute(sa.select(Vault).where(Vault.user_id == owner))
        ).scalar_one_or_none()
        if vault is None:
            continue
        try:
            key = service.require_key(owner)
        except service.VaultLocked:
            continue

        ready = (await session.execute(_ready_documents(import_session.id))).scalars().all()
        for document in ready:
            source = await session.get(SourceFile, document.source_file_id)
            try:
                result = await store.seal(session, document, source, vault.id, owner, key)
            except store.VaultRefused as error:
                # Left as it is, and said so. The next tick tries again; a file
                # that never becomes sealable stays visible on the import screen
                # as "awaiting", which is the honest state.
                await session.rollback()
                log.warning(
                    "could not seal %s from import %s: %s",
                    document.id, import_session.id, error,
                )
                continue
            await record(
                session, entity_type="document", entity_id=document.id, action="vault_in",
                actor_type=ActorType.SYSTEM, actor_id=owner,
                after={"import_session": str(import_session.id), "bytes": result.byte_size},
            )
            await session.commit()
            sealed += 1
    if sealed:
        log.info("vault sweep sealed %s document(s) from vault-bound imports", sealed)
    return sealed


async def run_forever(stopping: asyncio.Event, session_factory) -> None:
    """The background task. Quiet when there is nothing to do, which is almost
    always: the query is cheap and the interval is short enough that a person
    watching the import sees files seal as they finish."""
    while not stopping.is_set():
        try:
            async with session_factory() as session:
                await sweep_once(session)
        except Exception:  # a bad tick must not end the task
            log.exception("vault sweep failed; will try again")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=INTERVAL_SECONDS)
        except TimeoutError:
            pass
