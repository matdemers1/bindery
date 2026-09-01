"""The vault, over HTTP (T-16.9, REQ-185).

Two rules shape every route here.

**A locked vault says as little as possible.** `GET /vault` tells you whether
one exists and whether it is open, because the UI has to render something — and
nothing else. No counts, no titles, no sizes.

**Nothing here is reachable by an API token.** Unlocking is a thing a person did
at a keyboard with a PIN; a long-lived bearer token is the opposite, and
`Scope` refuses it by construction.
"""

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType
from api.db.models import AppUser, Document, SourceFile, VaultItem
from api.db.session import get_session
from api.schemas import (
    VaultItemOut,
    VaultSearchHitOut,
    VaultSearchOut,
    VaultStateOut,
)
from api.vault import crypto, service, store
from api.vault import search as vault_search
from api.vault.session import sessions

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
    pin: str | None = Body(None, embed=True),
    passphrase: str | None = Body(None, embed=True),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> VaultStateOut:
    vault = await _vault_or_404(session, user)
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
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
    except crypto.VaultError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error

    await record(
        session, entity_type="vault", entity_id=user.id, action="vault_unlocked",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        after={"with": "passphrase" if passphrase else "pin"},
    )
    await session.commit()
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
        out.append(
            VaultItemOut(
                document_id=item.document_id,
                title=meta.get("title"),
                original_filename=meta.get("original_filename"),
                byte_size=item.byte_size,
                page_count=item.page_count,
                vaulted_at=item.vaulted_at,
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

    document = (
        await session.execute(sa.select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    source = (
        await session.execute(
            sa.select(SourceFile).where(SourceFile.id == document.source_file_id)
        )
    ).scalar_one_or_none()
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


@router.get("/items/{document_id}/original")
async def original(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The decrypted original, for viewing or downloading while unlocked."""
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

    try:
        payload = store.open_object(item.object_name, document_id, key)
    except store.VaultRefused as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from error
    return Response(
        content=payload,
        media_type=item.original_media_type or "application/octet-stream",
        headers={
            # No caching anywhere. A decrypted vault document sitting in a
            # browser or proxy cache outlives the unlock that authorised it.
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Content-Disposition": "inline",
        },
    )
