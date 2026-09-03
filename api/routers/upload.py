import uuid
from collections.abc import AsyncIterator
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import events, ingest, quota
from api.auth.dependencies import current_user
from api.backlog import walker
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

    # The two ingest doors have to agree on what the archive accepts. The
    # importer refuses an unsupported suffix by walking past it; this one
    # accepted anything and handed it to OCR and to headless LibreOffice, which
    # is a conversion toolchain reachable with arbitrary attacker-chosen bytes
    # (CR-120). Same set, one definition — `walker.SUPPORTED`.
    suffix = PurePosixPath(file.filename or "").suffix.lower()
    if suffix not in walker.SUPPORTED:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Bindery does not take {suffix or 'files without an extension'}. "
            "It archives scans, photographs, office documents and video.",
        )

    # Before the bytes are stored, not after. Content-addressed storage never
    # deletes, so a refusal issued after writing would cost exactly the space
    # it was refusing (REQ-141).
    #
    # Unconditional, where it used to sit inside `if declared:` (CR-120). The
    # size on a multipart part is whatever the client chose to send, so a client
    # that simply omitted it skipped the account's storage limit entirely and
    # streamed as much as nginx would carry. What the declaration is still good
    # for is refusing early, before a gigabyte crosses the wire.
    declared = getattr(file, "size", None) or 0
    try:
        usage = await quota.check(session, user, declared)
    except quota.QuotaExceeded as full:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(full)) from full

    # And the same limit again as the bytes arrive, because the declaration was
    # never evidence. `store_stream` removes its own temp file when the stream
    # raises, so a refusal here costs nothing on disk.
    ceiling = quota.MAX_UPLOAD_BYTES
    if usage.remaining_bytes is not None:
        ceiling = min(ceiling, usage.remaining_bytes)
    try:
        blob = await store_stream(quota.capped(_chunks(file), ceiling))
    except quota.TooLarge as full:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(full)) from full

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
        await events.publish(
            session,
            [events.Topic.FILES, events.Topic.JOBS],
            library_id=library_id,
            source_file_id=result.source_file.id,
            state="received",
        )
        await session.commit()
        await session.refresh(result.source_file)

    return UploadResult(
        source_file=SourceFileOut.model_validate(result.source_file),
        duplicate=result.duplicate,
    )
