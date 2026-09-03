"""The vault, over HTTP (T-16.9, REQ-185).

Two rules shape every route here.

**A locked vault says as little as possible.** `GET /vault` tells you whether
one exists and whether it is open, because the UI has to render something — and
nothing else. No counts, no titles, no sizes.

**Nothing here is reachable by an API token.** Unlocking is a thing a person did
at a keyboard with a PIN; a long-lived bearer token is the opposite, and
`Scope` refuses it by construction.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

import sqlalchemy as sa
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.auth import throttle
from api.auth.client import client_ip
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType
from api.db.models import AppUser, Document, SourceFile, VaultItem
from api.db.session import get_session
from api.schemas import (
    MediaMetadataOut,
    VaultItemOut,
    VaultSearchHitOut,
    VaultSearchOut,
    VaultStateOut,
)
from api.vault import chunked, crypto, guard, service, store
from api.vault import search as vault_search
from api.vault.session import sessions

log = logging.getLogger("bindery.vault")

router = APIRouter(prefix="/vault", tags=["vault"])


async def _vault_or_404(session: AsyncSession, user: AppUser):
    vault = await service.get(session, user.id)
    if vault is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no vault on this account")
    return vault


def _key_or_423(user: AppUser) -> bytes:
    key = sessions.key(user.id)
    if key is None:
        # 423 Locked, which is exactly what this is, and distinguishable from a
        # permissions problem by anything reading the status code.
        raise HTTPException(status.HTTP_423_LOCKED, "the vault is locked")
    return key


@router.get("", response_model=VaultStateOut)
async def state(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> VaultStateOut:
    """Whether a vault exists and whether it is open. Deliberately nothing else.

    A locked vault that reports how many documents it holds has already said
    something about them.
    """
    vault = await service.get(session, user.id)
    return VaultStateOut(
        exists=vault is not None,
        unlocked=sessions.is_unlocked(user.id),
        pin_enabled=bool(vault and vault.pin_wrapped),
        pin_failures=vault.pin_failures if vault else 0,
    )


@router.post("/setup", response_model=VaultStateOut)
async def setup(
    passphrase: str = Body(..., embed=True),
    pin: str = Body(..., embed=True),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> VaultStateOut:
    """Create the vault. The passphrase given here cannot be recovered."""
    try:
        await service.create(session, user.id, passphrase, pin)
    except crypto.VaultError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    await record(
        session, entity_type="vault", entity_id=user.id, action="vault_created",
        actor_type=ActorType.HUMAN, actor_id=user.id, after={"pin_enabled": True},
    )
    await session.commit()
    return await state(user=user, session=session)


@router.post("/unlock", response_model=VaultStateOut)
async def unlock(
    request: Request,
    pin: str | None = Body(None, embed=True),
    passphrase: str | None = Body(None, embed=True),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> VaultStateOut:
    vault = await _vault_or_404(session, user)
    ip = client_ip(request)

    # Before the derivation, not after: a refused attempt must cost no Argon2,
    # and at the vault's parameters that is 256 MiB and about a second each.
    # The PIN counts its own failures and destroys its wrapper at five; the
    # passphrase cannot be treated that way — destroying it destroys the vault —
    # so what stands in front of it is a refusal that expires. See
    # `api/vault/guard.py`.
    try:
        await guard.check(session, user.id, ip)
    except throttle.Throttled as limited:
        await session.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Try again shortly.",
            headers={"Retry-After": str(limited.retry_after)},
        ) from limited

    try:
        if passphrase:
            await service.unlock_with_passphrase(session, vault, passphrase)
        elif pin:
            await service.unlock_with_pin(session, vault, pin)
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a PIN or passphrase")
    except crypto.WrongSecret as error:
        # Committed even on failure: the failure counter is the point, and a
        # rollback would hand an attacker unlimited attempts.
        await guard.record(session, user.id, ip, succeeded=False)
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
    except crypto.VaultError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error

    await guard.record(session, user.id, ip, succeeded=True)
    await record(
        session, entity_type="vault", entity_id=user.id, action="vault_unlocked",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        after={"with": "passphrase" if passphrase else "pin"},
    )
    await session.commit()

    # Objects still in the one-message format are upgraded now, while the key
    # is in hand (T-18.2). A failure here is logged and leaves the item as it
    # was — still readable, still not range-capable — rather than failing the
    # unlock the person just performed.
    key = service.require_key(user.id)
    legacy = (
        await session.execute(
            sa.select(VaultItem).where(
                VaultItem.vault_id == vault.id, VaultItem.format_version < 2
            )
        )
    ).scalars().all()
    upgraded = 0
    for item in legacy:
        try:
            upgraded += await store.reseal(session, item, key)
            await session.commit()
        except (store.VaultRefused, crypto.WrongSecret) as error:
            await session.rollback()
            log.error("could not re-seal vault item %s: %s", item.document_id, error)
    if upgraded:
        log.info("re-sealed %s vault object(s) into the chunked format", upgraded)

    return await state(user=user, session=session)


@router.post("/lock", response_model=VaultStateOut)
async def lock(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> VaultStateOut:
    sessions.lock(user.id)
    return await state(user=user, session=session)


@router.post("/pin", response_model=VaultStateOut)
async def change_pin(
    pin: str = Body(..., embed=True),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> VaultStateOut:
    """Set a new PIN. Requires the vault to be open already, and re-encrypts
    nothing — only the wrapper around the key changes."""
    vault = await _vault_or_404(session, user)
    try:
        await service.set_pin(session, vault, _key_or_423(user), pin)
    except crypto.VaultError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    await session.commit()
    return await state(user=user, session=session)


@router.get("/items", response_model=list[VaultItemOut])
async def items(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[VaultItemOut]:
    key = _key_or_423(user)
    vault = await _vault_or_404(session, user)
    rows = (
        (
            await session.execute(
                sa.select(VaultItem)
                .where(VaultItem.vault_id == vault.id)
                .order_by(VaultItem.vaulted_at.desc())
            )
        )
        .scalars()
        .all()
    )
    out = []
    for item in rows:
        meta = store.open_meta(item, key)
        media_type = store.media_type_for(item, meta)
        filename = meta.get("original_filename")
        out.append(
            VaultItemOut(
                document_id=item.document_id,
                title=meta.get("title"),
                original_filename=filename,
                byte_size=item.byte_size,
                page_count=item.page_count,
                vaulted_at=item.vaulted_at,
                media_type=media_type,
                is_image=store.is_image(media_type, filename),
                is_video=store.is_video(media_type, filename),
                media=(
                    MediaMetadataOut.model_validate(meta["media"])
                    if meta.get("media")
                    else None
                ),
            )
        )
    return out


@router.post("/items/{document_id}", response_model=VaultItemOut)
async def move_in(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> VaultItemOut:
    """Move a document into the vault. **This destroys its plaintext.**"""
    key = _key_or_423(user)
    vault = await _vault_or_404(session, user)

    document = await repository.get_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    source = await repository.get_source_file(session, user.id, document.source_file_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    # Recorded *before* the move, with the title and hash the document had, so
    # "what happened to that?" stays answerable when both are gone from the row.
    before = {
        "title": document.title,
        "sha256": source.sha256,
        "original_filename": source.original_filename,
    }
    try:
        sealed = await store.seal(session, document, source, vault.id, user.id, key)
    except store.VaultRefused as error:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    await record(
        session, entity_type="document", entity_id=document_id, action="vault_move_in",
        actor_type=ActorType.HUMAN, actor_id=user.id, before=before,
        after={"pages_sealed": sealed.pages, "warnings": sealed.warnings},
    )
    await session.commit()
    return VaultItemOut(
        document_id=document_id,
        title=before["title"],
        original_filename=before["original_filename"],
        byte_size=sealed.byte_size,
        page_count=sealed.pages,
        vaulted_at=None,
        warnings=sealed.warnings,
    )


@router.delete("/items/{document_id}", response_model=VaultItemOut)
async def move_out(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> VaultItemOut:
    """Move a document back out, restoring the original bytes."""
    key = _key_or_423(user)
    vault = await _vault_or_404(session, user)

    item = (
        await session.execute(
            sa.select(VaultItem).where(
                VaultItem.document_id == document_id, VaultItem.vault_id == vault.id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in your vault")

    # The `VaultItem` above is a real check — it ties this document to *this*
    # caller's vault — but it was the only one, and `unseal` writes a decrypted
    # original back onto disk (CR-074). Three things were unasked:
    #
    #   - whether the row exists at all. `document.source_file_id` was read on
    #     the next line, so a stale id was an `AttributeError` and a 500 where
    #     the rest of the application answers 404;
    #   - whether the caller can still see the library it would be restored
    #     into. `api/moves.py` rewrites `library_id` on the file and its
    #     documents together, so a vaulted document can end up somewhere the
    #     person who sealed it is no longer a member of;
    #   - whether the row is still live. A superseded document is history, and
    #     restoring a plaintext under one puts bytes back beneath a row nothing
    #     reads.
    #
    # Asked through `Scope`'s own guards rather than by hand: they are the same
    # ones every other route uses, and `require_visible` answers 404 so a probe
    # cannot tell "not yours" from "not there" (ADR-005).
    document = (
        await session.execute(sa.select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    source = (
        None
        if document is None
        else (
            await session.execute(
                sa.select(SourceFile).where(SourceFile.id == document.source_file_id)
            )
        ).scalar_one_or_none()
    )
    if document is None or source is None or document.superseded_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in your vault")
    bound = await repository.scope_for(session, user.id)
    bound.require_visible(document.library_id)
    bound.require_visible(source.library_id)
    if document.vaulted_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in your vault")

    meta = store.open_meta(item, key)

    try:
        restored = await store.unseal(session, document, source, item, key)
    except store.VaultRefused as error:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    await record(
        session, entity_type="document", entity_id=document_id, action="vault_move_out",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        after={"pages_restored": restored.pages},
    )
    await session.commit()
    return VaultItemOut(
        document_id=document_id,
        title=meta.get("title"),
        original_filename=meta.get("original_filename"),
        byte_size=restored.byte_size,
        page_count=restored.pages,
        vaulted_at=None,
        warnings=restored.warnings,
    )


@router.get("/search", response_model=VaultSearchOut)
async def search(
    q: str,
    limit: int = 25,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> VaultSearchOut:
    key = _key_or_423(user)
    vault = await _vault_or_404(session, user)
    found = await vault_search.search(session, vault.id, q, key, limit=limit)
    return VaultSearchOut(
        query=q,
        total=found.total,
        pages_scanned=found.pages_scanned,
        elapsed_ms=found.elapsed_ms,
        slow=found.slow,
        hits=[
            VaultSearchHitOut(
                document_id=hit.document_id,
                title=hit.title,
                page_number=hit.page_number,
                snippet=hit.snippet,
            )
            for hit in found.hits
        ],
    )


def _parse_range(header: str, length: int) -> tuple[int, int] | None:
    """`bytes=A-B`, `bytes=A-`, or `bytes=-N`. Anything else, or anything
    unsatisfiable, and the caller sends the whole file — which is what a client
    that cannot read a 206 wanted anyway."""
    if not header.startswith("bytes=") or length == 0:
        return None
    spec = header[len("bytes="):].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            count = int(end_s)
            if count <= 0:
                return None
            return max(0, length - count), length - 1
        start = int(start_s)
        end = int(end_s) if end_s else length - 1
    except ValueError:
        return None
    end = min(end, length - 1)
    if start < 0 or start > end:
        return None
    return start, end


def _chunk_end(reader: chunked.Reader, start: int) -> int:
    """The last byte of the chunk-sized span beginning at `start`."""
    return min(start + reader.header.chunk_size, reader.length) - 1


async def _stream_chunks(reader: chunked.Reader, first: bytes) -> AsyncIterator[bytes]:
    """The whole file, one chunk at a time, decrypted off the event loop.

    `first` has already been read — a wrong key or a bad first chunk becomes a
    500 before the response starts, which it cannot once bytes are on the wire.
    """
    if first:
        yield first
    for start in range(len(first), reader.length, reader.header.chunk_size):
        yield await asyncio.to_thread(reader.read_range, start, _chunk_end(reader, start))


@router.get("/items/{document_id}/original")
async def original(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    range_header: str | None = Header(None, alias="Range"),
) -> Response:
    """The decrypted original, for viewing, downloading — or playing.

    Honours `Range` (REQ-192): a browser plays video by asking for byte ranges,
    and under the chunked format each one is answered by decrypting only the
    chunks it covers. The 423 for a locked vault comes first, before any chunk
    is touched — a range must never be a way to read one byte of a locked
    vault.
    """
    key = _key_or_423(user)
    vault = await _vault_or_404(session, user)
    item = (
        await session.execute(
            sa.select(VaultItem).where(
                VaultItem.document_id == document_id, VaultItem.vault_id == vault.id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in your vault")

    # Falls back to the filename rather than octet-stream, or a photograph
    # downloads instead of displaying — which is what every item sealed before
    # the `mime_type` fix would otherwise still do.
    media_type = (
        store.media_type_for(item, store.open_meta(item, key)) or "application/octet-stream"
    )
    headers = {
        # No caching anywhere. A decrypted vault document sitting in a browser
        # or proxy cache outlives the unlock that authorised it.
        "Cache-Control": "no-store, no-cache, must-revalidate, private",
        "Content-Disposition": "inline",
        "Accept-Ranges": "bytes",
    }

    try:
        reader = store.open_reader(item.object_name, document_id, key)
        if reader is None:
            # A v1 object: the whole file is the only thing it can give, which
            # is what it always cost. The re-seal on the next unlock retires it.
            # In a thread even so — the memory is v1's price, the frozen event
            # loop was never anybody's.
            return Response(
                content=await asyncio.to_thread(
                    store.open_object, item.object_name, document_id, key
                ),
                media_type=media_type, headers=headers,
            )

        if range_header and (span := _parse_range(range_header, reader.length)):
            start, end = span
            # Only the chunks covering the range are decrypted (ADR-013). A
            # 2 GB video seeked to the middle costs two chunks of memory.
            return Response(
                content=await asyncio.to_thread(reader.read_range, start, end),
                status_code=status.HTTP_206_PARTIAL_CONTENT,
                media_type=media_type,
                headers={
                    **headers,
                    "Content-Range": f"bytes {start}-{end}/{reader.length}",
                },
            )

        # No Range — a download link, an <img src>, curl. This is the common
        # path and it used to be the expensive one: the whole plaintext joined
        # into one `bytes` before a single byte was sent, which is precisely
        # what ADR-013 exists to avoid. Streamed a chunk at a time instead, so
        # a 2 GB video costs a chunk of memory here too.
        first = (
            await asyncio.to_thread(reader.read_range, 0, _chunk_end(reader, 0))
            if reader.length
            else b""
        )
    except store.VaultRefused as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from error
    except crypto.WrongSecret as error:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "the vault object did not decrypt"
        ) from error

    return StreamingResponse(
        _stream_chunks(reader, first),
        media_type=media_type,
        # Declared rather than chunked: a download needs a size, and a browser
        # will not scrub a video whose length it does not know.
        headers={**headers, "Content-Length": str(reader.length)},
    )
