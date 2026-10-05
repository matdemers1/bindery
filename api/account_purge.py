"""The purge that ends a deleted account's grace period (BND-T-23.3, BND-ADR-015).

The second of the two files REQ-090 allows to delete, beside `api/vault/store.py`, and argued for
in BND-ADR-015. It removes what a person asked to have removed — with a current code and their
host name typed out, a week ago, in a week when any administrator could have restored the account
— and nothing they did not:

- **goes:** the account's credentials, links and memberships; every library in which it is the
  only member, whatever its `kind`, and everything in it; every document in its own vault,
  wherever that document's library is — nobody else can see it and nobody else can decrypt it;
- **stays:** every library somebody else belongs to and every document in it that is not in this
  account's vault, including the ones this account added; the `app_user` row, as a tombstone the
  attribution columns and the append-only audit trail keep pointing at.

`tests/test_no_destructive_paths.py` names this file and asserts it is reached only from the
worker's purge loop. Rows go first, each account in its own transaction; files go after the
commit, because an orphaned file is harmless and a row pointing at a missing file is not — the
backup's ordering rule.

A blob is shared by content address, so "nothing holds this hash any more" is only true until the
next upload of the same bytes. Each blob is therefore released in a transaction of its own that
takes `blobs.lock_for_removal` first — the exclusive half of the lock every ingest holds from
before it looks for an existing blob until its row commits — and asks the question again under it,
then unlinks before letting go (BND-T-23.5). One blob per transaction, so the purge never waits
for a lock while holding another, and cannot deadlock against an import holding several.
"""

import logging
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import account_deletion
from api.artifacts import derived_for, purge_derived
from api.audit import record
from api.auth.passwords import hash_password
from api.db.enums import ActorType, JobState
from api.db.models import (
    ApiToken,
    AppUser,
    Asset,
    Classification,
    Correspondent,
    CorrespondentAlias,
    Document,
    DocumentAsset,
    DocumentTag,
    DocumentType,
    DuplicatePair,
    EventLog,
    FieldProvenance,
    FieldSource,
    ImportItem,
    ImportSession,
    Invitation,
    Job,
    Library,
    LoginAttempt,
    MediaMetadata,
    Membership,
    OidcIdentity,
    Page,
    PasswordResetCode,
    RecoveryCode,
    RefreshToken,
    RelayRegistration,
    Rule,
    SavedSearch,
    SourceFile,
    Tag,
    Vault,
    VaultItem,
    VaultPage,
)
from api.storage import blobs
from api.storage.blobs import blob_path
from api.vault.store import object_path

log = logging.getLogger("bindery.account_purge")


def tombstone_email(user_id: uuid.UUID) -> str:
    """What an address becomes, so the real one can be invited again."""
    return f"deleted-{user_id}@deleted.invalid"


@dataclass
class Purged:
    """What one account's purge removed: counts for the audit row, files for after the commit."""

    user_id: uuid.UUID
    libraries_removed: int = 0
    libraries_kept: int = 0
    documents_removed: int = 0
    files_removed: int = 0
    # sha256 -> the removed documents that had classification artifacts under it
    blobs: dict[str, set[uuid.UUID]] = field(default_factory=dict)
    vault_objects: list[str] = field(default_factory=list)


@dataclass
class PurgeReport:
    purged: list[Purged] = field(default_factory=list)
    deferred: list[uuid.UUID] = field(default_factory=list)
    failed: list[uuid.UUID] = field(default_factory=list)


async def purge_due(session: AsyncSession, *, now: datetime | None = None) -> PurgeReport:
    """Purge every account whose grace period has passed. `now` is the test clock.

    One transaction per account, so one bad row cannot hold the rest up — and a failure is logged
    as an error, which the diagnostics screen shows, rather than swallowed.
    """
    now = now or datetime.now(UTC)
    report = PurgeReport()
    for user_id in await account_deletion.due(session, now):
        try:
            purged = await _purge_one(session, user_id, now)
        except Exception:
            await session.rollback()
            log.exception("account purge failed for %s; it will be tried again", user_id)
            report.failed.append(user_id)
            continue
        if purged is None:
            await session.rollback()
            report.deferred.append(user_id)
            continue
        await session.commit()
        await _remove_files(session, purged)
        report.purged.append(purged)
        log.warning(
            "account %s purged: %d librar%s and %d document%s removed, %d shared librar%s kept",
            user_id,
            purged.libraries_removed, "y" if purged.libraries_removed == 1 else "ies",
            purged.documents_removed, "" if purged.documents_removed == 1 else "s",
            purged.libraries_kept, "y" if purged.libraries_kept == 1 else "ies",
        )
    return report


async def _ids(session: AsyncSession, statement: sa.Select) -> list[uuid.UUID]:
    return list((await session.execute(statement)).scalars())


async def _private_libraries(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[list[uuid.UUID], int]:
    """The libraries only this account could see, and how many of its libraries are shared.

    Shared means somebody else can reach it (BND-ADR-005): another membership, or a document in
    it that sits in somebody else's vault. `library.kind` is a label chosen at creation and is
    deliberately not consulted.
    """
    mine = await _ids(
        session, sa.select(Membership.library_id).where(Membership.user_id == user_id)
    )
    private: list[uuid.UUID] = []
    for library_id in mine:
        others = await session.scalar(
            sa.select(
                sa.exists().where(
                    Membership.library_id == library_id, Membership.user_id != user_id
                )
            )
        )
        vaulted_by_others = await session.scalar(
            sa.select(
                sa.exists().where(
                    Document.library_id == library_id,
                    Document.vaulted_by.is_not(None),
                    Document.vaulted_by != user_id,
                )
            )
        )
        if not others and not vaulted_by_others:
            private.append(library_id)
    return private, len(mine) - len(private)


async def _purge_one(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> Purged | None:
    # Locked and re-read, never taken from the identity map: an administrator may have restored
    # the account since `due` looked, and a cached row would not know.
    user = await session.get(AppUser, user_id, with_for_update=True, populate_existing=True)
    if user is None or not account_deletion.is_pending(user) or user.is_active:
        return None  # restored, or purged by another worker, since `due` looked

    purged = Purged(user_id=user_id)
    private, purged.libraries_kept = await _private_libraries(session, user_id)

    vault_ids = sa.select(Vault.id).where(Vault.user_id == user_id)
    in_my_vault = sa.select(VaultItem.document_id).where(VaultItem.vault_id.in_(vault_ids))
    documents = await _ids(
        session,
        sa.select(Document.id).where(
            sa.or_(
                Document.library_id.in_(private),
                Document.id.in_(in_my_vault),
                Document.vaulted_by == user_id,
            )
        ),
    )
    candidate_files = await _ids(
        session,
        sa.select(SourceFile.id).where(
            sa.or_(
                SourceFile.library_id.in_(private),
                SourceFile.id.in_(
                    sa.select(Document.source_file_id).where(Document.id.in_(documents))
                ),
            )
        ),
    )

    # A stage holding one of these files right now would write into rows that are about to go;
    # leave this account for the next pass rather than pull them out from under it. Only
    # `running` defers: a job queued for an AI key that never arrives would otherwise defer the
    # purge for ever.
    busy = await session.scalar(
        sa.select(
            sa.exists().where(
                Job.state == JobState.RUNNING.value,
                sa.or_(Job.source_file_id.in_(candidate_files), Job.document_id.in_(documents)),
            )
        )
    )
    if busy:
        log.info("account purge for %s deferred: a stage is running on its files", user_id)
        return None

    for document_id, sha in (
        await session.execute(
            sa.select(Document.id, SourceFile.sha256)
            .join(SourceFile, SourceFile.id == Document.source_file_id)
            .where(Document.id.in_(documents))
        )
    ).all():
        purged.blobs.setdefault(sha, set()).add(document_id)

    await _remove_documents(session, documents, purged)
    # A file goes once nothing is left on it: all of a private library's, and a vaulted
    # document's own file in a shared library (sealing refuses a file with live siblings).
    files = await _ids(
        session,
        sa.select(SourceFile.id).where(
            SourceFile.id.in_(candidate_files),
            ~sa.exists().where(Document.source_file_id == SourceFile.id),
        ),
    )
    for sha in (
        await session.execute(sa.select(SourceFile.sha256).where(SourceFile.id.in_(files)))
    ).scalars():
        purged.blobs.setdefault(sha, set())
    await _remove_source_files(session, files)
    purged.files_removed = len(files)
    await _remove_libraries(session, private)
    purged.libraries_removed = len(private)

    await _remove_account_rows(session, user)
    email = user.email
    user.email = tombstone_email(user.id)
    # A hash of a value nobody holds, so no password can ever match the row again.
    user.password_hash = hash_password(secrets.token_urlsafe(32))
    user.display_name = None
    user.totp_secret = None
    user.totp_confirmed_at = None
    user.totp_last_step = None
    user.is_admin = False
    user.locked_until = None
    user.deleted_at = now
    user.delete_after = None
    # The invitation that made the account named its address; the address is what goes.
    await session.execute(
        sa.update(Invitation)
        .where(Invitation.accepted_user_id == user.id)
        .values(email=user.email)
    )
    await session.execute(sa.delete(LoginAttempt).where(LoginAttempt.email == email))

    await record(
        session,
        entity_type="app_user",
        entity_id=user.id,
        action="account_purged",
        actor_type=ActorType.SYSTEM,
        # Counts, never names: the request already recorded who asked, and the address is the
        # thing being removed.
        after={
            "libraries_removed": purged.libraries_removed,
            "shared_libraries_kept": purged.libraries_kept,
            "documents_removed": purged.documents_removed,
            "files_removed": purged.files_removed,
            "decision": "BND-ADR-015",
        },
    )
    await session.flush()
    return purged


async def _remove_documents(
    session: AsyncSession, documents: list[uuid.UUID], purged: Purged
) -> None:
    if not documents:
        return
    items = sa.select(VaultItem.id).where(VaultItem.document_id.in_(documents))
    purged.vault_objects.extend(
        (
            await session.execute(
                sa.select(VaultItem.object_name).where(VaultItem.document_id.in_(documents))
            )
        ).scalars()
    )
    await session.execute(sa.delete(VaultPage).where(VaultPage.vault_item_id.in_(items)))
    await session.execute(sa.delete(VaultItem).where(VaultItem.document_id.in_(documents)))

    classifications = sa.select(Classification.id).where(
        Classification.document_id.in_(documents)
    )
    await session.execute(
        sa.delete(FieldProvenance).where(FieldProvenance.classification_id.in_(classifications))
    )
    await session.execute(
        sa.delete(Classification).where(Classification.document_id.in_(documents))
    )
    for model in (DocumentTag, DocumentAsset, FieldSource):
        await session.execute(sa.delete(model).where(model.document_id.in_(documents)))
    await session.execute(
        sa.delete(DuplicatePair).where(
            sa.or_(
                DuplicatePair.document_a_id.in_(documents),
                DuplicatePair.document_b_id.in_(documents),
            )
        )
    )
    await session.execute(sa.delete(Job).where(Job.document_id.in_(documents)))
    # Log lines name files and titles (BND-ADR-009 scopes them for that reason).
    await session.execute(sa.delete(EventLog).where(EventLog.document_id.in_(documents)))
    await session.execute(sa.delete(Document).where(Document.id.in_(documents)))
    purged.documents_removed = len(documents)


async def _remove_source_files(session: AsyncSession, files: list[uuid.UUID]) -> None:
    if not files:
        return
    await session.execute(sa.delete(Job).where(Job.source_file_id.in_(files)))
    await session.execute(sa.delete(Page).where(Page.source_file_id.in_(files)))
    await session.execute(sa.delete(MediaMetadata).where(MediaMetadata.source_file_id.in_(files)))
    # An import in a library that stays keeps its inventory; it just no longer points at a file.
    await session.execute(
        sa.update(ImportItem).where(ImportItem.source_file_id.in_(files)).values(source_file_id=None)
    )
    await session.execute(sa.delete(EventLog).where(EventLog.source_file_id.in_(files)))
    await session.execute(sa.delete(SourceFile).where(SourceFile.id.in_(files)))


async def _remove_libraries(session: AsyncSession, libraries: list[uuid.UUID]) -> None:
    if not libraries:
        return
    in_libraries = sa.select(ImportSession.id).where(ImportSession.library_id.in_(libraries))
    await session.execute(sa.delete(ImportItem).where(ImportItem.session_id.in_(in_libraries)))
    correspondents = sa.select(Correspondent.id).where(Correspondent.library_id.in_(libraries))
    await session.execute(
        sa.delete(CorrespondentAlias).where(CorrespondentAlias.correspondent_id.in_(correspondents))
    )
    # One statement per table: `merged_into_id` points within the same library, and Postgres
    # checks a NO ACTION reference at the end of the statement, not row by row.
    for model in (
        ImportSession,
        SavedSearch,
        Rule,
        DuplicatePair,
        Tag,
        Correspondent,
        DocumentType,
        Asset,
        EventLog,
        Membership,
    ):
        await session.execute(sa.delete(model).where(model.library_id.in_(libraries)))
    await session.execute(sa.delete(Library).where(Library.id.in_(libraries)))


async def _remove_account_rows(session: AsyncSession, user: AppUser) -> None:
    """Everything that was the account rather than something it made."""
    for model in (
        RelayRegistration,  # before the sessions and links it names
        RefreshToken,
        OidcIdentity,
        ApiToken,
        RecoveryCode,
        PasswordResetCode,
        SavedSearch,
        Vault,  # its items went with their documents
        Membership,
    ):
        await session.execute(sa.delete(model).where(model.user_id == user.id))


async def _remove_files(session: AsyncSession, purged: Purged) -> None:
    """After the commit: blobs nothing holds any more, their derived trees, vault objects.

    Errors are logged and never raised — the rows are gone, and a file left behind is an orphan
    the integrity check ignores, not a reference to something missing.
    """
    for name in purged.vault_objects:
        _unlink(object_path(name))  # random names, never shared: nothing else can reach one
    for sha, document_ids in purged.blobs.items():
        try:
            await _release_blob(session, sha, document_ids)
        except Exception:
            log.exception("could not release purged blob %s; it stays as an orphan", sha[:12])
        finally:
            await session.rollback()  # ends the transaction, which is what lets the lock go


async def _release_blob(
    session: AsyncSession, sha: str, document_ids: set[uuid.UUID]
) -> None:
    """Remove one blob if nothing holds it, under the lock an ingest of the same bytes takes.

    The caller ends the transaction, and with it the lock, after this returns: the unlink has to
    happen while the lock is held, or an upload could find the file in the gap and lose it.
    """
    await blobs.lock_for_removal(session, sha)
    # Asked under the lock, so it sees every row an ingest of these bytes committed before
    # letting go of its hold: a blob is another library's original when any row holds its hash,
    # whichever library that row is in.
    held = await session.scalar(sa.select(sa.exists().where(SourceFile.sha256 == sha)))
    if held:
        # Leave the blob and the derived renders, and take only this account's classification
        # artifacts out of it.
        classify = derived_for(sha).root / "classify"
        for document_id in document_ids:
            for path in _matching(classify, f"{document_id}-"):
                _unlink(path)
        return
    _unlink(blob_path(sha))
    if not purge_derived(sha):
        log.error("could not remove derived files for a purged blob %s", sha[:12])


def _matching(directory: Path, prefix: str) -> Iterable[Path]:
    try:
        return [path for path in directory.iterdir() if path.name.startswith(prefix)]
    except FileNotFoundError:
        return []


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.exception("could not remove %s after an account purge", path)
