"""Moving documents into and out of the vault (T-16.5 to T-16.7).

This is the module that destroys plaintext, and everything about its shape is
about making that safe:

    encrypt → write → read back → compare to the original hash → only then delete

Any other order can lose a document to a full disk or a crash between two
statements. The verify is not a formality — it is the whole difference between
a vault and a shredder, and it is why this module was written after the crypto
it depends on had property tests and before the UI that calls it existed.

The deletion is a deliberate exception to REQ-090, granted by ADR-012 and
permitted because it is neither automatic nor unattended: a person selected a
document and asked for exactly this. `tests/test_no_destructive_paths.py` names
this file as the single exempt one and asserts the exemption does not spread.

What survives is the document row, the audit event with the title and hash it
had, and the ciphertext — so "what happened to that?" stays answerable forever.
"""

import hashlib
import json
import logging
import mimetypes
import os
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import artifacts
from api.config import get_settings
from api.db.enums import JobState
from api.db.models import Document, DocumentTag, Job, Page, SourceFile, VaultItem, VaultPage
from api.storage.blobs import blob_path
from api.vault import crypto

log = logging.getLogger("bindery.vault")

# What the vault shows as pictures rather than as rows. Matches the photo
# wall's list, because "is this a photograph" should not have two answers.
IMAGE_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".heic", ".heif",
)

# Stages that are still going to want the plaintext. Vaulting something mid-
# pipeline would seal a half-read document and leave jobs pointing at bytes
# that no longer exist, so it is refused rather than raced.
IN_FLIGHT = (JobState.QUEUED, JobState.RUNNING, JobState.FAILED)


class VaultRefused(Exception):
    """The move cannot proceed, and nothing has been changed."""


@dataclass
class Sealed:
    document_id: uuid.UUID
    object_name: str
    byte_size: int
    pages: int
    warnings: list[str] = field(default_factory=list)


def vault_root() -> Path:
    return get_settings().data_root / "vault" / "objects"


def object_path(object_name: str) -> Path:
    # Two levels of fan-out like the blob store, so a large vault does not put
    # ten thousand entries in one directory.
    return vault_root() / object_name[:2] / object_name[2:4] / object_name


def new_object_name() -> str:
    """Random, never the plaintext hash.

    A content address is an existence oracle: anyone holding a copy of a file
    could confirm the archive holds it without decrypting anything. That is the
    leak migration 0017 closed and T-13.7 found again, and it is the one place
    where the vault must *not* follow the archive's own convention.
    """
    return secrets.token_hex(32)


async def _refuse_if_busy(session: AsyncSession, source_file_id: uuid.UUID) -> None:
    busy = (
        await session.execute(
            sa.select(sa.func.count(Job.id)).where(
                Job.source_file_id == source_file_id,
                Job.state.in_([state.value for state in IN_FLIGHT]),
            )
        )
    ).scalar_one()
    if busy:
        raise VaultRefused(
            "this document is still being processed. Vaulting it now would seal "
            "a half-read file and leave the pipeline pointing at bytes that no "
            "longer exist — wait for it to finish, then try again."
        )


def _write_atomically(path: Path, payload: bytes) -> None:
    """Write beside, fsync, then rename.

    A half-written ciphertext that the verify step then reads back correctly
    from the page cache, followed by a delete, is a way to lose a document to a
    power cut. The rename is the atomic part.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, payload)
        os.fsync(handle)
    finally:
        os.close(handle)
    os.replace(temporary, path)


async def seal(
    session: AsyncSession,
    document: Document,
    source: SourceFile,
    vault_id: uuid.UUID,
    owner_id: uuid.UUID,
    data_key: bytes,
) -> Sealed:
    """Move one document into the vault.

    Encrypts the original, its page text and its metadata; verifies the
    ciphertext reads back byte-identical to the source; and only then removes
    the plaintext, the derived renders and the page rows.
    """
    await _refuse_if_busy(session, source.id)

    plaintext_path = blob_path(source.sha256)
    if not plaintext_path.is_file():
        raise VaultRefused(
            f"the original for {source.original_filename!r} is not on disk, so "
            "there is nothing to encrypt. Run an integrity check before doing "
            "anything else with this document."
        )
    original = plaintext_path.read_bytes()

    object_name = new_object_name()
    key = crypto.file_key(data_key, object_name.encode())
    # The document id is authenticated alongside the bytes, so a ciphertext
    # cannot be moved onto another document's row and still decrypt.
    associated = str(document.id).encode()

    target = object_path(object_name)
    _write_atomically(target, crypto.encrypt(original, key, associated=associated))

    # Read back from disk rather than trusting the buffer we just encrypted:
    # the question is whether *the file* can be decrypted, not whether the
    # function is deterministic.
    try:
        recovered = crypto.decrypt(target.read_bytes(), key, associated=associated)
        verified = hashlib.sha256(recovered).hexdigest() == source.sha256
    except crypto.WrongSecret:
        # By far the likelier corruption: a flipped bit fails the AEAD tag
        # rather than decrypting to different plaintext. Catching only the hash
        # mismatch would let the common case escape as a raw crypto error and
        # leave the half-written object behind, so both land here.
        verified = False
    if not verified:
        target.unlink(missing_ok=True)
        raise VaultRefused(
            "the encrypted copy did not read back as the original. Nothing was "
            "deleted and the document is untouched."
        )

    pages = (
        (
            await session.execute(
                sa.select(Page)
                .where(Page.source_file_id == source.id)
                .order_by(Page.page_number)
            )
        )
        .scalars()
        .all()
    )

    meta = {
        "title": document.title,
        "original_filename": source.original_filename,
        # `source.mime_type`, not `media_type`. The first version read the
        # wrong attribute through `getattr(..., None)`, which swallowed the
        # typo silently: every vaulted item got a null type, every download
        # was served as application/octet-stream, and photographs downloaded
        # instead of displaying.
        "media_type": source.mime_type,
        "page_start": document.page_start,
        "page_end": document.page_end,
        "document_date": document.document_date.isoformat()
        if getattr(document, "document_date", None)
        else None,
    }

    item = VaultItem(
        document_id=document.id,
        vault_id=vault_id,
        object_name=object_name,
        byte_size=len(original),
        sealed_sha256=crypto.encrypt(source.sha256.encode(), data_key),
        sealed_meta=crypto.encrypt(json.dumps(meta).encode(), data_key),
        original_media_type=source.mime_type,
        page_count=len(pages),
    )
    session.add(item)
    await session.flush()

    for page in pages:
        session.add(
            VaultPage(
                vault_item_id=item.id,
                page_number=page.page_number,
                sealed_text=crypto.encrypt((page.text or "").encode(), data_key),
            )
        )

    # --- past this line the plaintext starts going away -------------------

    document.vaulted_by = owner_id
    # The title lives in `sealed_meta` from here. Leaving it on the row would
    # mean a locked archive still knew what the document was called, which is
    # most of what a title is for.
    document.title = None

    # Taxonomy does not travel into the vault. A tag is a library-wide row, so
    # leaving the link would let anyone browsing tags see that *something*
    # tagged "medical" exists and is unreachable. Revoked rather than deleted,
    # the same pattern a cross-library move uses.
    await session.execute(
        sa.update(DocumentTag)
        .where(DocumentTag.document_id == document.id, DocumentTag.removed_at.is_(None))
        .values(removed_at=datetime.now(UTC))
    )
    document.correspondent_id = None
    document.document_type_id = None
    document.known_form_id = None

    for page in pages:
        await session.delete(page)

    warnings: list[str] = []
    # Derived renders and thumbnails are reproducible from the original, and the
    # original is about to be ciphertext — so they are plaintext copies of a
    # vaulted document and must go.
    removed = artifacts.purge_derived(source.sha256)
    if removed is False:
        warnings.append("some derived renders could not be removed; check the data root")

    plaintext_path.unlink(missing_ok=True)
    log.info("vaulted document %s (%s bytes)", document.id, len(original))
    return Sealed(document.id, object_name, len(original), len(pages), warnings)


def open_object(object_name: str, document_id: uuid.UUID, data_key: bytes) -> bytes:
    """The original bytes, for a caller that has already been authorised."""
    path = object_path(object_name)
    if not path.is_file():
        raise VaultRefused(f"the vault object for {document_id} is missing from disk")
    key = crypto.file_key(data_key, object_name.encode())
    return crypto.decrypt(path.read_bytes(), key, associated=str(document_id).encode())


def open_meta(item: VaultItem, data_key: bytes) -> dict:
    return json.loads(crypto.decrypt(item.sealed_meta, data_key).decode())


def media_type_for(item: VaultItem, meta: dict) -> str | None:
    """What this is, so a browser renders it rather than downloading it.

    Falls back to the filename when the column is null, which covers every
    item sealed before the `mime_type` typo above was fixed. Guessing from an
    extension is weak in general and exactly right here: the alternative is
    `application/octet-stream`, under which a photograph is a file you save
    rather than a picture you look at.
    """
    if item.original_media_type:
        return item.original_media_type
    filename = meta.get("original_filename")
    if not filename:
        return None
    return mimetypes.guess_type(filename)[0]


def is_image(media_type: str | None, filename: str | None) -> bool:
    """Whether the vault should show this as a picture.

    The media type first; the extension when there is none. HEIC is included
    because an iPhone camera roll is most of what anyone vaults, and Safari
    renders it natively even where other browsers do not.
    """
    if media_type and media_type.startswith("image/"):
        return True
    if filename:
        return filename.lower().endswith(IMAGE_SUFFIXES)
    return False


def open_pages(pages: list[VaultPage], data_key: bytes) -> dict[int, str]:
    return {
        page.page_number: crypto.decrypt(page.sealed_text, data_key).decode()
        for page in pages
    }


async def unseal(
    session: AsyncSession,
    document: Document,
    source: SourceFile,
    item: VaultItem,
    data_key: bytes,
) -> Sealed:
    """Move a document back out of the vault (T-16.6, REQ-182).

    The mirror image, with the same ordering discipline: the plaintext is
    written and verified against the hash recorded before vaulting *before* the
    document is marked visible again. A document that reappears in the archive
    pointing at bytes that are not there would be worse than one that stayed
    hidden.

    The ciphertext is deliberately left in place. Removing it is a second
    destructive act with nothing to gain — the plaintext already exists again,
    and an orphaned encrypted object costs disk rather than safety.
    """
    original = open_object(item.object_name, document.id, data_key)
    expected = crypto.decrypt(item.sealed_sha256, data_key).decode()
    actual = hashlib.sha256(original).hexdigest()
    if actual != expected:
        raise VaultRefused(
            "the vaulted copy does not match the hash recorded when it went in. "
            "Nothing has been changed; this needs looking at before the document "
            "is trusted."
        )
    if actual != source.sha256:
        raise VaultRefused(
            "this vault object does not belong to that source file. Nothing has "
            "been changed."
        )

    destination = blob_path(source.sha256)
    _write_atomically(destination, original)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != source.sha256:
        destination.unlink(missing_ok=True)
        raise VaultRefused("the restored original did not verify; it has been removed")
    # Originals are immutable and read-only everywhere else in the archive
    # (invariant 1), and a file coming back out of the vault is no exception.
    os.chmod(destination, 0o444)

    meta = open_meta(item, data_key)
    document.title = meta.get("title")
    document.vaulted_by = None

    # Pages come back so the document is searchable again. Their text is the
    # same text; re-OCR would be a second reading of the same bytes and could
    # legitimately differ, which would make the archive's history a lie.
    sealed_pages = (
        (
            await session.execute(
                sa.select(VaultPage)
                .where(VaultPage.vault_item_id == item.id)
                .order_by(VaultPage.page_number)
            )
        )
        .scalars()
        .all()
    )
    for number, text in open_pages(list(sealed_pages), data_key).items():
        session.add(Page(source_file_id=source.id, page_number=number, text=text))

    for page in sealed_pages:
        await session.delete(page)
    # Flush the page deletes before the item goes. There is no ORM relationship
    # between the two, so the unit of work has no dependency to order them by
    # and is free to issue the parent delete first — which the database then
    # refuses on the foreign key.
    await session.flush()
    await session.delete(item)

    log.info("unvaulted document %s", document.id)
    return Sealed(
        document.id,
        item.object_name,
        len(original),
        len(sealed_pages),
        ["derived renders were not restored; they rebuild on the next pipeline run"],
    )
