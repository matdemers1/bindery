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
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import AppUser, Membership, SourceFile

log = logging.getLogger("bindery.quota")


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
    """
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
