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

import logging
import random
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api import ingest
from api.db.enums import (
    ActorType,
    ImportItemState,
    ImportState,
    IngestSource,
)
from api.db.models import Document, ImportItem, ImportSession
from api.storage.blobs import CHUNK_SIZE, store_stream
from worker.backlog.dryrun import analyse
from worker.backlog.walker import walk

log = logging.getLogger("bindery.import")


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

    result = walk(root)
    report, cost = await analyse(session, root, result)

    # One row per file, keyed on path, so re-scanning converges.
    for path in result.files:
        await session.execute(
            insert(ImportItem)
            .values(id=uuid.uuid4(), session_id=import_session.id, path=str(path),
                    byte_size=path.stat().st_size if path.exists() else None,
                    state=ImportItemState.PENDING.value)
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

    done = 0
    for item in items:
        path = Path(item.path)
        try:
            if not path.is_file():
                item.state = ImportItemState.FAILED
                item.error = "file disappeared between the scan and the import"
                continue

            blob = await store_stream(_file_chunks(path))
            item.sha256 = blob.sha256

            result = await ingest.register(
                session, blob,
                library_id=import_session.library_id,
                ingest_source=IngestSource.BULK_IMPORT,
                original_filename=path.name,
                mime_type=None,
                actor_type=ActorType.SYSTEM,
                metadata={"import_session": str(import_session.id), "source_path": str(path)},
            )
            item.source_file_id = result.source_file.id
            item.state = (
                ImportItemState.DUPLICATE if result.duplicate else ImportItemState.INGESTED
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
