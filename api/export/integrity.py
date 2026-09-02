"""Integrity checking (T-6.5, REQ-095).

Bit rot is silent. A blob can decay on disk for years and nothing will notice,
because nothing reads a 2019 tax return between 2019 and the day you need it.
By then every backup has faithfully replicated the damage.

So the check runs *before* the backup, and its job is to answer one question
honestly: **does every original still hash to what we recorded?** Blobs are
content-addressed, so the file's own name is the expected digest — there is no
separate checksum table to drift out of sync.

Three failure modes, reported separately because they mean different things:

- **missing** — the row exists, the file does not. Something deleted it.
- **corrupt** — the file exists and hashes differently. Bit rot, or a bad write.
- **orphan** — a blob no data references. Harmless (backup ordering produces
  these by design) and *never* removed automatically: REQ-090 has no exceptions,
  and an "orphan" is also what an interrupted ingest looks like from here.
- **sealed** — the original is in the private vault, so its plaintext is
  deliberately gone (Phase 16). Not missing, and reported separately because
  calling it missing makes a working archive look like a damaged one — and
  `run_backup` and `replicate` both *refuse to run* over a failing integrity
  check. A false alarm here does not just mislead, it stops the backups.

  Their ciphertext is checked instead: a vault object that is gone from disk
  really is missing, and nothing else in the archive can replace it.

  "Sealed" is a property of the **file**, and only when *every* live document
  over it is vaulted. A file with one vaulted document and two ordinary ones is
  still read in the clear by those two, so its plaintext is still required and
  its absence is real loss — see `check` (CR-024).
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Document, SourceFile, VaultItem
from api.storage.blobs import blob_path

log = logging.getLogger("bindery.integrity")

# Big enough that hashing is not syscall-bound, small enough that a 400MB scan
# does not become a 400MB resident buffer.
_CHUNK = 1024 * 1024


@dataclass
class IntegrityReport:
    started_at: datetime
    finished_at: datetime | None = None
    checked: int = 0
    bytes_read: int = 0
    ok: int = 0
    missing: list[dict] = field(default_factory=list)
    corrupt: list[dict] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    # Originals that are encrypted rather than absent. Listed, never counted
    # against health — the plaintext being gone is the feature working.
    sealed: list[dict] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.missing and not self.corrupt

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "checked": self.checked,
            "bytes_read": self.bytes_read,
            "ok": self.ok,
            "healthy": self.healthy,
            "missing": self.missing,
            "corrupt": self.corrupt,
            "sealed": self.sealed,
            # Orphans are informational. They are listed, never acted on.
            "orphan_count": len(self.orphans),
            "orphans": self.orphans[:200],
        }


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


async def check(
    session: AsyncSession,
    *,
    library_ids: list[uuid.UUID] | None = None,
    include_orphans: bool = True,
) -> IntegrityReport:
    """Re-hash every original and compare against its content address."""
    from api.vault import store

    report = IntegrityReport(started_at=datetime.now(UTC))

    # How many documents currently read each source file. The count is the
    # denominator of "is this file sealed": the plaintext is one file shared by
    # every live document over it, so it is only deliberately gone when all of
    # them are in the vault. Superseded rows are history and read nothing.
    live_documents: dict[uuid.UUID, int] = dict(
        (
            await session.execute(
                sa.select(Document.source_file_id, sa.func.count(Document.id))
                .where(Document.superseded_at.is_(None))
                .group_by(Document.source_file_id)
            )
        ).all()
    )

    # Every vault object over each file, not one of them. Keying this on the
    # file and keeping only the last name meant a file with two vaulted
    # documents had exactly one ciphertext checked and the other never looked
    # at — and a file with one vaulted document out of three was called sealed
    # outright, so its missing original was never noticed and the backups ran
    # over it (CR-024).
    sealed_objects: dict[uuid.UUID, list[str]] = {}
    for row in (
        await session.execute(
            sa.select(Document.source_file_id, VaultItem.object_name)
            .join(VaultItem, VaultItem.document_id == Document.id)
            .where(
                Document.vaulted_by.is_not(None),
                Document.superseded_at.is_(None),
            )
        )
    ).all():
        sealed_objects.setdefault(row.source_file_id, []).append(row.object_name)

    query = sa.select(
        SourceFile.id, SourceFile.sha256, SourceFile.original_filename, SourceFile.byte_size
    )
    if library_ids is not None:
        query = query.where(SourceFile.library_id.in_(library_ids))

    known: set[str] = set()
    for row in (await session.execute(query.order_by(SourceFile.received_at))).all():
        known.add(row.sha256)
        report.checked += 1
        path = blob_path(row.sha256)
        record = {
            "source_file_id": str(row.id),
            "sha256": row.sha256,
            "original_filename": row.original_filename,
        }

        sealed_here = sealed_objects.get(row.id, [])
        live_here = live_documents.get(row.id, 0)
        # Every ciphertext over this file has to be here, whether or not the
        # file as a whole counts as sealed — all of them, not the one the old
        # per-file map happened to keep. Unlike a blob, nothing can reproduce
        # one.
        lost = [
            name for name in sealed_here if not store.object_path(name).is_file()
        ]
        for name in lost:
            log.error(
                "integrity: VAULT OBJECT MISSING for %s (%s) — the encrypted "
                "copy is gone and there is no other",
                name[:12], row.original_filename,
            )
            report.missing.append({**record, "vault_object": name})

        if sealed_here and len(sealed_here) >= live_here:
            # Every live document over this file is in the vault, so the
            # plaintext is supposed to be gone. Nothing left to hash.
            for name in sealed_here:
                if name not in lost:
                    report.sealed.append({**record, "vault_object": name})
            if not lost:
                report.ok += 1
            continue

        if not path.is_file():
            if sealed_here:
                # A partly vaulted file. The unsealed documents are still read
                # from this plaintext, so it is not deliberately gone — it is
                # lost, and every one of those documents with it. This is the
                # state the old per-file `sealed` map reported as healthy.
                unsealed = live_here - len(sealed_here)
                log.error(
                    "integrity: blob missing for %s (%s) — only %s of %s documents "
                    "on this file are vaulted, so the original was still needed",
                    row.sha256, row.original_filename, len(sealed_here), live_here,
                )
                report.missing.append({
                    **record,
                    "reason": (
                        "partially vaulted: the original is gone but "
                        f"{unsealed} document(s) on this file are not sealed"
                    ),
                })
            else:
                log.error(
                    "integrity: blob missing for %s (%s)",
                    row.sha256, row.original_filename,
                )
                report.missing.append(record)
            continue

        # Off the loop: re-hashing the archive is a 400 MB read per original and
        # minutes in total. On the worker's single loop that froze the OCR
        # slots, the inbox watcher and the health monitor for the length of
        # every replication run — including the health monitor that exists to
        # report a stalled pipeline (CR-011).
        actual, size = await asyncio.to_thread(hash_file, path)
        report.bytes_read += size
        if actual != row.sha256:
            # This is the case that matters. Say it loudly — the whole point of
            # checking before backup is that a silent corruption becomes a
            # replicated corruption.
            log.error(
                "integrity: CORRUPT %s — hashes to %s (%s)",
                row.sha256, actual, row.original_filename,
            )
            report.corrupt.append({**record, "actual_sha256": actual, "size": size})
            continue
        report.ok += 1

    if include_orphans and library_ids is None:
        # An rglob of the whole blob tree, for the same reason as the hashing.
        report.orphans = sorted(await asyncio.to_thread(_scan_orphans, known))

    report.finished_at = datetime.now(UTC)
    log.info(
        "integrity: %s checked, %s ok, %s missing, %s corrupt, %s orphaned, "
        "%s sealed in the vault",
        report.checked, report.ok, len(report.missing), len(report.corrupt),
        len(report.orphans), len(report.sealed),
    )
    return report


def _scan_orphans(known: set[str]) -> list[str]:
    """Blobs on disk that nothing references. Listed, never touched (REQ-090)."""
    from api.config import get_settings

    root = get_settings().blob_root
    if not root.is_dir():
        return []
    found = []
    for path in root.rglob("*"):
        if path.is_file() and path.name not in known and len(path.name) == 64:
            found.append(path.name)
    return found
