import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType, IngestSource
from api.db.models import AppUser, SourceFile
from api.db.session import get_session
from api.schemas import SourceFileOut, UploadResult
from api.storage.blobs import CHUNK_SIZE, store_stream

router = APIRouter(tags=["ingest"])


async def _chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(CHUNK_SIZE):
        yield chunk


@router.post("/upload", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
async def upload(
    response: Response,
    library_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UploadResult:
    """Store an original and record it. No processing (T-0.8).

    Phase 1 replaces the tail of this with a queued ingest job; for now the file
    lands in the blob store and gets a row, which is all the exit demo needs.
    """
    if not await repository.can_write_library(session, user.id, library_id):
        # Same answer whether the library is missing or merely not the caller's:
        # membership is not a thing to probe for.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")

    result = await store_stream(_chunks(file))

    existing = await repository.get_source_file_by_hash(session, result.sha256)
    if existing is not None:
        # Exact duplicate. The content address already resolves to these bytes,
        # so there is nothing to store and nothing to record — and nothing was
        # created, so this is a 200 rather than the route's default 201.
        response.status_code = status.HTTP_200_OK
        return UploadResult(source_file=SourceFileOut.model_validate(existing), duplicate=True)

    source_file = SourceFile(
        library_id=library_id,
        sha256=result.sha256,
        byte_size=result.byte_size,
        mime_type=file.content_type,
        original_filename=file.filename,
        ingest_source=IngestSource.WEB_UPLOAD,
        ingest_metadata={"uploaded_by": str(user.id)},
    )
    session.add(source_file)
    await session.flush()

    await record(
        session,
        entity_type="source_file",
        entity_id=source_file.id,
        action="ingest",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={
            "sha256": result.sha256,
            "byte_size": result.byte_size,
            "original_filename": file.filename,
            "library_id": str(library_id),
        },
    )
    await session.commit()
    await session.refresh(source_file)

    return UploadResult(source_file=SourceFileOut.model_validate(source_file), duplicate=False)
