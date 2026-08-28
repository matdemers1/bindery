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
from api.db.models import Document, Job, Library, Membership, Page, SourceFile

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
    """Live documents the user can see. Superseded segments are history."""
    library_ids = await visible_library_ids(session, user_id)
    if not library_ids:
        return []
    result = await session.execute(
        sa.select(Document)
        .where(Document.library_id.in_(library_ids), Document.superseded_at.is_(None))
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


async def get_document(
    session: AsyncSession, user_id: uuid.UUID, document_id: uuid.UUID
) -> Document | None:
    """A live document, or None if it does not exist *or* is not the caller's."""
    library_ids = await visible_library_ids(session, user_id)
    if not library_ids:
        return None
    result = await session.execute(
        sa.select(Document).where(
            Document.id == document_id,
            Document.library_id.in_(library_ids),
            Document.superseded_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


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


async def get_source_file(
    session: AsyncSession, user_id: uuid.UUID, source_file_id: uuid.UUID
) -> SourceFile | None:
    """A source file, or None if it does not exist *or* is not the caller's.

    Deliberately one answer for both cases: whether a document exists in a
    library you cannot see is itself information.
    """
    library_ids = await visible_library_ids(session, user_id)
    if not library_ids:
        return None
    result = await session.execute(
        sa.select(SourceFile).where(
            SourceFile.id == source_file_id, SourceFile.library_id.in_(library_ids)
        )
    )
    return result.scalar_one_or_none()


async def get_page(
    session: AsyncSession, user_id: uuid.UUID, source_file_id: uuid.UUID, page_number: int
) -> Page | None:
    library_ids = await visible_library_ids(session, user_id)
    if not library_ids:
        return None
    result = await session.execute(
        sa.select(Page)
        .join(SourceFile, SourceFile.id == Page.source_file_id)
        .where(
            Page.source_file_id == source_file_id,
            Page.page_number == page_number,
            SourceFile.library_id.in_(library_ids),
        )
    )
    return result.scalar_one_or_none()


async def list_pages(
    session: AsyncSession, user_id: uuid.UUID, source_file_id: uuid.UUID
) -> Sequence[Page]:
    library_ids = await visible_library_ids(session, user_id)
    if not library_ids:
        return []
    result = await session.execute(
        sa.select(Page)
        .join(SourceFile, SourceFile.id == Page.source_file_id)
        .where(Page.source_file_id == source_file_id, SourceFile.library_id.in_(library_ids))
        .order_by(Page.page_number)
    )
    return result.scalars().all()


def visible_jobs(library_ids: list[uuid.UUID]):
    """A jobs query already narrowed to the caller's libraries.

    A job reaches a library through **either** key. This used to join only
    through `source_file`, on the reasoning that every job had one — which was
    true when it was written and stopped being true the moment the classify
    stage began enqueueing by `document_id` alone.

    The consequence was not a leak but the opposite, and worse for it: every
    classification failure became invisible on the pipeline screen and could
    not be retried, so a document that failed AI review simply sat there with
    nothing to show it had. Outer joins, so a job is reachable by whichever
    key it carries, and one with neither is still excluded rather than leaked.
    """
    return (
        sa.select(Job)
        .outerjoin(SourceFile, SourceFile.id == Job.source_file_id)
        .outerjoin(Document, Document.id == Job.document_id)
        .where(
            sa.or_(
                SourceFile.library_id.in_(library_ids),
                Document.library_id.in_(library_ids),
            )
        )
    )
