"""Data access, scoped to the caller's visible libraries.

Invariant 4 / ADR-005 / REQ-101: permission filtering happens *here*, never at
individual call sites. Routers ask for "the documents this user can see" and are
given a query that is already constrained. Phase 7 hardens this with the leak
suite; the seam exists from the baseline so there is never a call site that
learned to do its own filtering.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.enums import MembershipRole
from api.db.models import Document, Library, Membership, SourceFile

WRITE_ROLES = (MembershipRole.OWNER, MembershipRole.CONTRIBUTOR)


async def visible_library_ids(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Every library the user holds any membership in."""
    result = await session.execute(
        sa.select(Membership.library_id).where(Membership.user_id == user_id)
    )
    return list(result.scalars().all())


async def writable_library_ids(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    result = await session.execute(
        sa.select(Membership.library_id).where(
            Membership.user_id == user_id, Membership.role.in_(WRITE_ROLES)
        )
    )
    return list(result.scalars().all())


async def can_write_library(
    session: AsyncSession, user_id: uuid.UUID, library_id: uuid.UUID
) -> bool:
    return library_id in await writable_library_ids(session, user_id)


async def list_libraries(session: AsyncSession, user_id: uuid.UUID) -> Sequence[Library]:
    result = await session.execute(
        sa.select(Library)
        .join(Membership, Membership.library_id == Library.id)
        .where(Membership.user_id == user_id)
        .order_by(Library.name)
    )
    return result.scalars().all()


async def list_documents(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> Sequence[Document]:
    """Documents the user can see. Empty until segmentation lands in Phase 2."""
    library_ids = await visible_library_ids(session, user_id)
    if not library_ids:
        return []
    result = await session.execute(
        sa.select(Document)
        .where(Document.library_id.in_(library_ids))
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


async def list_source_files(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> Sequence[SourceFile]:
    library_ids = await visible_library_ids(session, user_id)
    if not library_ids:
        return []
    result = await session.execute(
        sa.select(SourceFile)
        .where(SourceFile.library_id.in_(library_ids))
        .order_by(SourceFile.received_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


async def get_source_file_by_hash(
    session: AsyncSession, sha256: str
) -> SourceFile | None:
    """Hash lookup is global on purpose — the unique constraint is global."""
    result = await session.execute(sa.select(SourceFile).where(SourceFile.sha256 == sha256))
    return result.scalar_one_or_none()
