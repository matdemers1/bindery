import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import ingest
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType, IngestSource
from api.db.models import AppUser
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
    """Store an original, record it, and queue it for OCR."""
    if not await repository.can_write_library(session, user.id, library_id):
        # Same answer whether the library is missing or merely not the caller's:
        # membership is not a thing to probe for.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")

    blob = await store_stream(_chunks(file))
    result = await ingest.register(
        session,
        blob,
        library_id=library_id,
        ingest_source=IngestSource.WEB_UPLOAD,
        original_filename=file.filename,
        mime_type=file.content_type,
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        metadata={"uploaded_by": str(user.id)},
    )

    if result.duplicate:
        # Nothing was created, so this is a 200 rather than the route's 201.
        response.status_code = status.HTTP_200_OK
    else:
        await session.commit()
        await session.refresh(result.source_file)

    return UploadResult(
        source_file=SourceFileOut.model_validate(result.source_file),
        duplicate=result.duplicate,
    )
