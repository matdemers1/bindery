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
from api.db.enums import ActorType, ImportItemState, JobState, SourceFileState
from api.db.models import Document, ImportItem, ImportSession, Job, SourceFile, Vault
from api.segments import live
from api.vault import store
from api.vault.session import sessions

log = logging.getLogger("bindery.vault.sweep")

INTERVAL_SECONDS = 15
IN_FLIGHT = tuple(state.value for state in (JobState.QUEUED, JobState.RUNNING, JobState.FAILED))


def _finished_and_unvaulted():
    """What a document must be before this may seal it.

    `is_not(None)` on the busy subquery is load-bearing: classify and rules jobs
    carry a document and no source file, and `x NOT IN (…, NULL)` is never true
    in SQL. One queued classification anywhere in the archive — the ordinary
    state during any import — therefore made this list empty and the sweep
    sealed nothing, silently, for as long as the pipeline had work to do.
    """
    busy = sa.select(Job.source_file_id).where(
        Job.state.in_(IN_FLIGHT), Job.source_file_id.is_not(None)
    )
    return (
        # Only the files this import actually brought in. A DUPLICATE item
        # points at the source file that was *already* in the archive, so
        # importing a re-downloaded statement with "straight into the vault"
        # ticked sealed the copy filed months ago: its title moved into
        # `sealed_meta` and was blanked, its correspondent, type and known-form
        # links cleared, its tags revoked, and it left the archive. Nobody asked
        # for that document to be vaulted; they asked for the ones they brought.
        ImportItem.state == ImportItemState.INGESTED.value,
        live(),
        Document.vaulted_by.is_(None),
        SourceFile.state == SourceFileState.PROCESSED,
        SourceFile.id.not_in(busy),
    )


def _ready_documents(import_session_id: uuid.UUID):
    """Documents from this import that are finished and not yet vaulted."""
    return (
        sa.select(Document)
        .join(SourceFile, SourceFile.id == Document.source_file_id)
        .join(ImportItem, ImportItem.source_file_id == SourceFile.id)
        .where(ImportItem.session_id == import_session_id, *_finished_and_unvaulted())
    )


def _ready_for_owner(owner_id: uuid.UUID):
    """The same question across every vault-bound import one person owns.

    One query per unlocked person, rather than one per import that has ever been
    bound to the vault. The number of those only goes up, and the sweep asks
    four times a minute for as long as the process is running.
    """
    return (
        sa.select(Document.id, Document.source_file_id, ImportItem.session_id)
        .join(SourceFile, SourceFile.id == Document.source_file_id)
        .join(ImportItem, ImportItem.source_file_id == SourceFile.id)
        .join(ImportSession, ImportSession.id == ImportItem.session_id)
        .where(
            ImportSession.to_vault.is_(True),
            ImportSession.created_by == owner_id,
            *_finished_and_unvaulted(),
        )
        .distinct()
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
    """One pass over every vault-bound import whose owner is unlocked.

    Works from plain ids throughout. A refused seal rolls the session back,
    which expires every ORM object loaded before it — the import row, the vault
    row, the documents — and touching any of them afterwards fails. The first
    version did exactly that, so one file with a missing blob broke the whole
    tick, every tick, and nothing else in the import ever sealed.
    """
    owners = (
        await session.execute(
            sa.select(ImportSession.created_by)
            .where(ImportSession.to_vault.is_(True), ImportSession.created_by.is_not(None))
            .distinct()
        )
    ).scalars().all()

    # `peek`, not `key` or `is_unlocked`: this runs every fifteen seconds
    # whether anyone is here or not, and a read that extended the idle window
    # would mean an account with one vault-bound import never idled out at all.
    # The vault closing behind you is the whole point of the timeout (ADR-012);
    # a poll is not somebody using the vault. Every owner is still asked, so an
    # expired key is still dropped on a tick nobody is watching.
    unlocked = [
        (owner, key) for owner in owners if (key := sessions.peek(owner)) is not None
    ]
    if not unlocked:
        # The ordinary state, and now the whole cost of it: one narrow select
        # and nothing else. Asking each import in turn meant a vault lookup and
        # a three-way join per import that had *ever* been bound, four times a
        # minute, to rediscover that they all finished months ago.
        return 0

    sealed = 0
    for owner, key in unlocked:
        vault_id = (
            await session.execute(sa.select(Vault.id).where(Vault.user_id == owner))
        ).scalar_one_or_none()
        if vault_id is None:
            continue

        ready = [
            (row.id, row.source_file_id, row.session_id)
            for row in (await session.execute(_ready_for_owner(owner))).all()
        ]
        for document_id, source_file_id, import_id in ready:
            document = await session.get(Document, document_id)
            source = await session.get(SourceFile, source_file_id)
            if document is None or source is None:
                continue
            try:
                result = await store.seal(session, document, source, vault_id, owner, key)
            except store.VaultRefused as error:
                # Left as it is, and said so. The next tick tries again; a file
                # that never becomes sealable stays visible on the import screen
                # as "awaiting", which is the honest state.
                await session.rollback()
                log.warning(
                    "could not seal %s from import %s: %s", document_id, import_id, error
                )
                continue
            await record(
                session, entity_type="document", entity_id=document_id, action="vault_in",
                actor_type=ActorType.SYSTEM, actor_id=owner,
                after={"import_session": str(import_id), "bytes": result.byte_size},
            )
            await session.commit()
            sealed += 1
    if sealed:
        log.info("vault sweep sealed %s document(s) from vault-bound imports", sealed)
    return sealed


async def run_forever(stopping: asyncio.Event, session_factory) -> None:
    """The background task. Quiet when there is nothing to do, which is almost
    always: a tick with no vault open costs one narrow select, and the interval
    is short enough that a person watching the import sees files seal as they
    finish."""
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
