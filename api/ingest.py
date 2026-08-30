"""Turning stored bytes into a tracked source file.

Every adapter — web upload, watched folder, camera, bulk import — converges here
(Architecture L3): each one produces bytes plus a provenance record, and from
this point the pipeline does not care which door the file came through.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api import queue
from api.audit import record
from api.db import repository
from api.db.enums import ActorType, IngestSource, JobStage, SourceFileState
from api.db.models import SourceFile
from api.storage.blobs import BlobWriteResult


@dataclass(frozen=True)
class IngestResult:
    source_file: SourceFile
    # True when these exact bytes were already in the archive. The caller is
    # told rather than silently handed a second row (REQ-005).
    duplicate: bool


async def register(
    session: AsyncSession,
    blob: BlobWriteResult,
    *,
    library_id: uuid.UUID,
    ingest_source: IngestSource,
    original_filename: str | None,
    mime_type: str | None,
    actor_type: ActorType,
    actor_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> IngestResult:
    """Record a stored blob as a source file and queue it for processing.

    Does not commit — the caller owns the transaction, so the source-file row,
    its audit event, and its normalize job land together or not at all.
    """
    existing = await repository.get_source_file_by_hash(
        session, blob.sha256, library_id
    )
    if existing is not None:
        return IngestResult(source_file=existing, duplicate=True)

    source_file = SourceFile(
        library_id=library_id,
        sha256=blob.sha256,
        byte_size=blob.byte_size,
        mime_type=mime_type,
        original_filename=original_filename,
        ingest_source=ingest_source,
        # Provenance on every file (REQ-008).
        ingest_metadata=metadata or {},
        state=SourceFileState.RECEIVED,
    )
    session.add(source_file)
    await session.flush()

    await record(
        session,
        entity_type="source_file",
        entity_id=source_file.id,
        action="ingest",
        actor_type=actor_type,
        actor_id=actor_id,
        after={
            "sha256": blob.sha256,
            "byte_size": blob.byte_size,
            "original_filename": original_filename,
            "ingest_source": ingest_source.value,
            "library_id": str(library_id),
        },
    )

    await queue.enqueue(session, JobStage.NORMALIZE, source_file_id=source_file.id)
    return IngestResult(source_file=source_file, duplicate=False)
