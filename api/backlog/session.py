"""Running an import, resumably (REQ-086).

Every step is idempotent and every step writes its state, so an import that is
killed mid-run resumes rather than restarts. This is not defensive
over-engineering: an import of a real archive takes hours, and anything that
takes hours will be interrupted.

Backlog documents are flagged `is_backlog`, which keeps them out of the daily
review queue entirely (REQ-083). That flag is the concrete mitigation for
**R-03**, the risk rated most likely to end the project: five thousand documents
arriving in the queue you use every day turns triage into the new mess.
"""

import asyncio
import logging
import random
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api import ingest, quota
from api.backlog.dryrun import analyse
from api.backlog.walker import walk
from api.db.enums import (
    ActorType,
    ImportItemState,
    ImportState,
    IngestSource,
    MembershipRole,
)
from api.db.models import AppUser, Document, ImportItem, ImportSession, Membership
from api.storage.blobs import CHUNK_SIZE, store_stream

log = logging.getLogger("bindery.import")

# Rows per INSERT during a scan. Large enough that 20,000 files cost twenty
# round trips rather than twenty thousand; small enough that one statement's
# parameters stay well inside what the driver will bind.
INSERT_BATCH = 1000


async def _file_chunks(path: Path):
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            yield chunk


async def scan(session: AsyncSession, import_session: ImportSession) -> ImportSession:
    """Walk, analyse, and record. Ingests nothing."""
    root = Path(import_session.root_path)
    if not root.is_dir():
        import_session.state = ImportState.FAILED
        import_session.last_error = f"{root} is not a directory the worker can see"
        await session.flush()
        return import_session

    # Off the event loop. `walk` is a synchronous traversal of a tree meant to
    # hold tens of thousands of files, and it stats every one of them — for a
    # real archive that is tens of thousands of blocking syscalls on the api's
    # only event loop, inside a request that already has a transaction open,
    # with every other request queued behind it (CR-113). It touches no
    # session and no ORM object, only the filesystem, so a thread is all it
    # needs.
    result = await asyncio.to_thread(walk, root)
    report, cost = await analyse(session, root, result)

    # One row per file, keyed on path, so re-scanning converges — in batches,
    # because this is the entry point for absorbing tens of thousands of files
    # and a statement per file is that many sequential round trips inside one
    # open transaction, with every other request queued behind it. The size
    # comes from the walk, which already stat()'d every one of these paths.
    for chunk in range(0, len(result.files), INSERT_BATCH):
        rows = [
            {
                "id": uuid.uuid4(),
                "session_id": import_session.id,
                "path": str(path),
                "byte_size": result.sizes.get(path),
                "state": ImportItemState.PENDING.value,
            }
            for path in result.files[chunk : chunk + INSERT_BATCH]
        ]
        await session.execute(
            insert(ImportItem)
            .values(rows)
            .on_conflict_do_nothing(index_elements=[ImportItem.session_id, ImportItem.path])
        )

    import_session.dry_run = report.to_json()
    import_session.cost_estimate = cost.to_json()
    import_session.state = ImportState.DRY_RUN
    await session.flush()

    log.info(
        "scanned %s: %s files, %s pages, ~$%.2f batched%s",
        root, report.total_files, report.estimated_pages, cost.batch_usd,
        "  ** OVER THE R-06 ALARM **" if cost.exceeds_alarm else "",
    )
    return import_session


async def select_sample(session: AsyncSession, import_session: ImportSession) -> int:
    """Pick a stratified sample for pass one (REQ-084).

    Stratified by directory, not random across the whole set: a backlog is
    organised by *something*, even if only by year, and one folder's worth of
    documents is not representative of the archive's taxonomy.
    """
    items = (
        await session.execute(
            sa.select(ImportItem).where(
                ImportItem.session_id == import_session.id,
                ImportItem.state == ImportItemState.PENDING.value,
            )
        )
    ).scalars().all()
    if not items:
        return 0

    by_directory: dict[str, list[ImportItem]] = {}
    for item in items:
        by_directory.setdefault(str(Path(item.path).parent), []).append(item)

    rng = random.Random(str(import_session.id))  # deterministic per session
    chosen: list[ImportItem] = []
    per_directory = max(1, import_session.sample_size // max(len(by_directory), 1))
    for group in by_directory.values():
        rng.shuffle(group)
        chosen.extend(group[:per_directory])

    chosen = chosen[: import_session.sample_size]
    for item in chosen:
        item.state = ImportItemState.SAMPLED

    import_session.state = ImportState.SAMPLING
    await session.flush()
    log.info(
        "sampled %s of %s files across %s directories",
        len(chosen), len(items), len(by_directory),
    )
    return len(chosen)


async def charged_account(
    session: AsyncSession, import_session: ImportSession
) -> AppUser | None:
    """The account this import's bytes are charged to.

    `api/routers/upload.py` checks the uploader's storage quota before it
    stores anything; this door checked nobody's, so pointing the importer at a
    large folder walked straight past the limit that exists because the Zima
    has one disk pool and the failure mode when it fills is that OCR, backups
    and Postgres stop for every household.

    Whoever started the import is the analogue of the uploader, and it is what
    `api/vault/sweep.py` already resolves an import by. A row from before that
    column existed falls back to the library's owner — the account whose
    `usage_for` total these files land in either way.
    """
    if import_session.created_by is not None:
        found = await session.get(AppUser, import_session.created_by)
        if found is not None:
            return found
    return (
        await session.execute(
            sa.select(AppUser)
            .join(Membership, Membership.user_id == AppUser.id)
            .where(
                Membership.library_id == import_session.library_id,
                Membership.role == MembershipRole.OWNER,
            )
            .order_by(AppUser.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def ingest_batch(
    session: AsyncSession,
    import_session: ImportSession,
    *,
    states: list[ImportItemState],
    limit: int = 50,
) -> int:
    """Ingest up to `limit` files. Safe to call repeatedly; that is the resume."""
    items = (
        await session.execute(
            sa.select(ImportItem)
            .where(
                ImportItem.session_id == import_session.id,
                ImportItem.state.in_([state.value for state in states]),
            )
            .order_by(ImportItem.path)
            .limit(limit)
        )
    ).scalars().all()

    # Checked again here, against the root the scan was authorised for: the
    # per-item path is a stored string, and the ingest is what actually opens
    # it. A row that points somewhere else is refused rather than read.
    root = Path(import_session.root_path).resolve()

    # Resolved once for the batch. `None` means there is nobody to charge, and
    # this refuses rather than importing free of charge — a library with no
    # owner is a state `api/routers/household.py` will not create.
    account = await charged_account(session, import_session)

    done = 0
    for item in items:
        path = Path(item.path)
        try:
            if account is None:
                item.state = ImportItemState.FAILED
                item.error = (
                    "this import has no account to charge its storage against"
                )
                continue
            if not path.resolve().is_relative_to(root):
                item.state = ImportItemState.FAILED
                item.error = "outside the folder that was scanned"
                continue
            if not path.is_file():
                item.state = ImportItemState.FAILED
                item.error = "file disappeared between the scan and the import"
                continue

            # The same order the upload door uses: ask before the bytes are
            # stored, because content-addressed storage never deletes and a
            # refusal issued afterwards costs exactly the space it refused.
            # Per item rather than per batch — `quota.check` re-reads the total
            # each time and holds its advisory lock for the whole transaction,
            # so file 40 is measured against the 39 in front of it.
            try:
                usage = await quota.check(session, account, item.byte_size or 0)
            except quota.QuotaExceeded as full:
                item.state = ImportItemState.FAILED
                item.error = str(full)[:500]
                continue

            # And again as the bytes arrive, because the size in the row is
            # what the scan saw and the file may have grown since.
            # `store_stream` removes its own temp file when the stream raises.
            chunks = _file_chunks(path)
            if usage.remaining_bytes is not None:
                chunks = quota.capped(chunks, usage.remaining_bytes)
            try:
                blob = await store_stream(chunks)
            except quota.TooLarge as full:
                item.state = ImportItemState.FAILED
                item.error = str(full)[:500]
                continue
            item.sha256 = blob.sha256

            # Everything decided so far goes in before the savepoint opens —
            # this item's hash, and every earlier item's outcome. Left pending,
            # the next statement's autoflush would write them *inside* the
            # savepoint, and rolling it back would take them with it.
            await session.flush()

            # A savepoint, so that one file's database error is one file's.
            # `register` inserts and flushes; a unique violation racing another
            # importer, or any connection-level error, leaves the session in a
            # failed transaction, and without this every later statement in the
            # loop raised `PendingRollbackError` and the closing flush took the
            # route down with a 500 — losing the whole batch's work *and* every
            # FAILED marker explaining why. The marker below is written after
            # the savepoint has been rolled back, so it survives.
            async with session.begin_nested():
                result = await ingest.register(
                    session, blob,
                    library_id=import_session.library_id,
                    ingest_source=IngestSource.BULK_IMPORT,
                    original_filename=path.name,
                    mime_type=None,
                    actor_type=ActorType.SYSTEM,
                    metadata={
                        "import_session": str(import_session.id), "source_path": str(path)
                    },
                )
                source_file_id = result.source_file.id
                duplicate = result.duplicate
            item.source_file_id = source_file_id
            item.state = (
                ImportItemState.DUPLICATE if duplicate else ImportItemState.INGESTED
            )
            done += 1
        except Exception as exc:  # one bad file must not end the import
            log.exception("import failed for %s", path)
            item.state = ImportItemState.FAILED
            item.error = repr(exc)[:500]
        finally:
            item.updated_at = sa.func.now()

    await session.flush()
    return done


async def mark_backlog(session: AsyncSession, import_session: ImportSession) -> int:
    """Flag every document this import produced.

    **This is the R-03 mitigation.** Backlog documents never enter the daily
    review queue; they get their own surface with a lower auto-file bar, because
    they were already unfindable and anything is an improvement.
    """
    result = await session.execute(
        sa.update(Document)
        .where(
            Document.source_file_id.in_(
                sa.select(ImportItem.source_file_id).where(
                    ImportItem.session_id == import_session.id,
                    ImportItem.source_file_id.is_not(None),
                )
            ),
            Document.is_backlog.is_(False),
        )
        .values(is_backlog=True)
    )
    return result.rowcount or 0


async def progress(session: AsyncSession, import_session: ImportSession) -> dict[str, int]:
    rows = (
        await session.execute(
            sa.select(ImportItem.state, sa.func.count())
            .where(ImportItem.session_id == import_session.id)
            .group_by(ImportItem.state)
        )
    ).all()
    return {getattr(state, "value", str(state)): count for state, count in rows}
