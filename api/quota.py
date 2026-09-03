"""Storage quotas (T-10.15, REQ-141).

The Zima has one disk pool and is about to hold other households' archives. A
single bulk import can fill it, and the failure mode when it does is not "that
user cannot upload" — it is that OCR stops, backups stop, and Postgres stops,
for everyone.

Usage is measured over **distinct blobs**, because storage is
content-addressed: the same file uploaded twice occupies one blob and should
count once. It is also measured over the account's *libraries*, not its
uploads, so a document that arrives by watched folder counts the same as one
dropped on the page.

The refusal names the limit and the current usage. "Quota exceeded" with no
numbers is a support conversation; "you have used 4.7 GB of 5 GB" is an answer.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import AppUser, Membership, SourceFile

log = logging.getLogger("bindery.quota")

# The API's own ceiling on a single file. nginx caps a request body at 512M
# (infra/nginx.conf) and until now that was the *only* size limit anywhere —
# per-request rather than per-account, and absent entirely for anything that
# reaches uvicorn without passing through nginx. Set to the same figure, so
# nothing a person can upload today is refused tomorrow.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

# The namespace half of the two-int advisory lock key, so a later use of
# `pg_advisory_xact_lock` for something else cannot collide with this one by
# choosing the same account-derived number. "QUOT".
LOCK_NAMESPACE = 0x51554F54


@dataclass(frozen=True)
class Usage:
    used_bytes: int
    quota_bytes: int | None
    files: int

    @property
    def unlimited(self) -> bool:
        return self.quota_bytes is None

    @property
    def remaining_bytes(self) -> int | None:
        if self.quota_bytes is None:
            return None
        return max(0, self.quota_bytes - self.used_bytes)


class TooLarge(Exception):
    """One file past a hard byte limit, discovered while it was streaming.

    Separate from `QuotaExceeded` because the two are different answers: one
    says "not on this account", the other says "not at all, at this size".
    Both are 413 at the door.
    """

    def __init__(self, limit: int, seen: int) -> None:
        self.limit = limit
        self.seen = seen
        super().__init__(
            f"This file is larger than the {_human(limit)} this upload may use, "
            "and was refused part-way through. Nothing was stored."
        )


class QuotaExceeded(Exception):
    def __init__(self, usage: Usage, incoming: int) -> None:
        self.usage = usage
        self.incoming = incoming
        super().__init__(
            f"This would take you past your storage limit. "
            f"You have used {_human(usage.used_bytes)} of "
            f"{_human(usage.quota_bytes or 0)}, and this file is "
            f"{_human(incoming)}."
        )


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _lock_key(user_id: uuid.UUID) -> int:
    """An account, as a positive signed 32-bit integer."""
    return user_id.int % 0x7FFFFFFF


async def capped(chunks: AsyncIterator[bytes], limit: int) -> AsyncIterator[bytes]:
    """The same bytes, refused the moment they pass `limit`.

    The declared size on a multipart part is a claim by the client, and an
    absent one used to mean no check at all (CR-120). This counts what actually
    arrives, so the limit holds whatever the client said — and it raises
    *during* the stream, before the temp file is moved into the content-
    addressed store, which is the only point at which a refusal is still free.
    """
    seen = 0
    async for chunk in chunks:
        seen += len(chunk)
        if seen > limit:
            raise TooLarge(limit, seen)
        yield chunk


async def usage_for(session: AsyncSession, user: AppUser) -> Usage:
    """What this account is holding, counting each blob once."""
    libraries = sa.select(Membership.library_id).where(Membership.user_id == user.id)
    # DISTINCT on the hash: two source_file rows pointing at identical bytes
    # share one blob on disk, and charging twice for it would be wrong.
    distinct = (
        sa.select(SourceFile.sha256, sa.func.max(SourceFile.byte_size).label("bytes"))
        .where(SourceFile.library_id.in_(libraries))
        .group_by(SourceFile.sha256)
        .subquery()
    )
    row = (
        await session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(distinct.c.bytes), 0),
                sa.func.count(),
            ).select_from(distinct)
        )
    ).one()
    return Usage(
        used_bytes=int(row[0] or 0),
        quota_bytes=user.storage_quota_bytes,
        files=int(row[1] or 0),
    )


async def check(session: AsyncSession, user: AppUser, incoming_bytes: int) -> Usage:
    """Raise `QuotaExceeded` if this upload would take the account past its limit.

    Checked *before* the bytes are stored. Storing first and refusing after
    would leave the blob on disk — content-addressed storage never deletes, so
    the refusal would cost exactly the space it was refusing.

    **Serialised per account** (CR-076). This is a read followed by a write, and
    `usage_for` is a plain aggregate: two requests that both read 4.7 GB of a
    5 GB limit both decided they had room, and both stored. That is not an edge
    case here — the backlog importer and the watched-folder walker both ingest
    in parallel, and a browser posts several files at once — and the
    consequence when the pool fills is the one this module's docstring names.

    A transaction-scoped advisory lock is the cheapest thing that fixes it: it
    is held until the caller's transaction ends, which is *after* the
    `source_file` row is inserted, so the next request through reads a total
    that already includes the file before it. Keyed on the account, so two
    households never wait on each other, and released by the transaction ending
    however it ends — no reservation row to leak if the process dies mid-upload.
    """
    await session.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                sa.literal(LOCK_NAMESPACE, sa.Integer),
                # Postgres wants an int4 pair; a UUID is 128 bits. A collision
                # costs two accounts a little waiting and nothing else, which
                # is why truncating is safe here and would not be in a key.
                sa.literal(_lock_key(user.id), sa.Integer),
            )
        )
    )
    usage = await usage_for(session, user)
    if usage.quota_bytes is None:
        return usage
    if usage.used_bytes + incoming_bytes > usage.quota_bytes:
        log.warning(
            "refused %s bytes for %s: %s of %s used",
            incoming_bytes, user.email, usage.used_bytes, usage.quota_bytes,
        )
        raise QuotaExceeded(usage, incoming_bytes)
    return usage


async def usage_by_account(session: AsyncSession) -> list[dict]:
    """Per-account storage, for the admin panel (REQ-142).

    Counts and bytes only. An administrator can see that an account holds 312
    files and 4.7 GB; they cannot see what any of them are (ADR-009).

    Deduplicated per account rather than per library: content-addressed storage
    means the same blob in two of your libraries is one file on disk. The first
    version of this used a correlated LATERAL that SQLAlchemy quietly turned
    into a cartesian product — every account was reported as holding the entire
    archive, which is both wrong and alarming.
    """
    # One row per (account, blob) first, so a blob is charged once per account
    # however many libraries or source_file rows point at it.
    per_blob = (
        sa.select(
            Membership.user_id.label("user_id"),
            SourceFile.sha256.label("sha256"),
            sa.func.max(SourceFile.byte_size).label("bytes"),
        )
        .join(SourceFile, SourceFile.library_id == Membership.library_id)
        .group_by(Membership.user_id, SourceFile.sha256)
        .subquery()
    )
    totals = (
        sa.select(
            per_blob.c.user_id,
            sa.func.sum(per_blob.c.bytes).label("used_bytes"),
            sa.func.count().label("files"),
        )
        .group_by(per_blob.c.user_id)
        .subquery()
    )

    rows = (
        await session.execute(
            sa.select(
                AppUser.id,
                AppUser.email,
                AppUser.display_name,
                AppUser.storage_quota_bytes,
                sa.func.coalesce(totals.c.used_bytes, 0),
                sa.func.coalesce(totals.c.files, 0),
            ).outerjoin(totals, totals.c.user_id == AppUser.id)
        )
    ).all()
    return [
        {
            "user_id": row[0],
            "email": row[1],
            "display_name": row[2],
            "quota_bytes": row[3],
            "used_bytes": int(row[4] or 0),
            "files": int(row[5] or 0),
        }
        for row in rows
    ]
