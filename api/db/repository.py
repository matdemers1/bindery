"""Data access, scoped to the caller's visible libraries.

Invariant 4 / ADR-005 / REQ-101: permission filtering happens *here*, never at
individual call sites. Routers ask for "the documents this user can see" and are
given a query that is already constrained. Phase 7 hardens this with the leak
suite; the seam exists from the baseline so there is never a call site that
learned to do its own filtering.

This module is the entry point for routes that hold a `user_id` rather than a
`Scope`, and it is **not** a second implementation of the boundary. It resolves
a `Scope` (narrowed to the caller's token) and asks that for its queries. It did
build its own once, and the cost is on the record: the vault clause was written
here and in `api/db/scope.py`, only one copy was updated, and five read paths
went straight past it (`api/vault/boundary.py`). One filter, two doors.
"""

import contextvars
import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import scope as scoping
from api.db.models import Document, Job, Library, Membership, Page, SourceFile

# `WRITE_ROLES` used to be declared here as well as in `api/db/scope.py`, which
# is the shape this module exists to stop having: one rule, two copies, and no
# way to notice when they disagree.


@dataclass(frozen=True)
class TokenBoundary:
    """What a bearer token narrows its owner down to, for one request."""

    user_id: uuid.UUID
    library_ids: frozenset[uuid.UUID]
    may_write: bool


# `None` for a browser session and for the worker, which is why the auth layer
# *sets* it on every authenticated request rather than leaving it alone.
_token_boundary: contextvars.ContextVar[TokenBoundary | None] = contextvars.ContextVar(
    "bindery_token_boundary", default=None
)


def bind_token_boundary(boundary: TokenBoundary | None) -> None:
    """Narrow the rest of this request to what its bearer token may reach.

    The narrowing lives here for the same reason the library filter does
    (invariant 4, ADR-005): nineteen routers ask this module for "the libraries
    this user can see", and a second, narrower question asked at each of those
    call sites is one that will be forgotten at one of them. It was — every
    router but `household` handed a token everything its creator could reach.
    """
    _token_boundary.set(boundary)


def _within_the_token(
    user_id: uuid.UUID, library_ids: list[uuid.UUID], *, writing: bool = False
) -> list[uuid.UUID]:
    """`library_ids`, intersected with the caller's token if there is one.

    Only ever narrows. A token is a subset of the person who issued it, so a
    boundary this returns is never wider than the memberships it was given, and
    a caller asking about somebody else is left alone.
    """
    boundary = _token_boundary.get()
    if boundary is None or boundary.user_id != user_id:
        return library_ids
    if writing and not boundary.may_write:
        return []
    return [lid for lid in library_ids if lid in boundary.library_ids]


async def scope_for(session: AsyncSession, user_id: uuid.UUID) -> scoping.Scope:
    """This caller's boundary, narrowed to their token, as a `Scope`.

    The one function in this module that talks to `api/db/scope.py`, and the
    reason everything below is a two-line delegation rather than a query with a
    filter on it. A route that needs a shape this module does not offer should
    take this and call `scope.only(Model)`, so it gets the whole boundary rather
    than the half it remembered.
    """
    resolved = await scoping.resolve(session, user_id)
    return dataclasses.replace(
        resolved,
        visible=tuple(_within_the_token(user_id, list(resolved.visible))),
        writable=tuple(_within_the_token(user_id, list(resolved.writable), writing=True)),
    )


async def visible_library_ids(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Every library the user holds any membership in."""
    return list((await scope_for(session, user_id)).visible)


async def writable_library_ids(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    return list((await scope_for(session, user_id)).writable)


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
    libraries = list(result.scalars().all())
    reachable = set(_within_the_token(user_id, [library.id for library in libraries]))
    return [library for library in libraries if library.id in reachable]


async def list_documents(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> Sequence[Document]:
    """Live documents the user can see. Superseded segments are history."""
    bound = await scope_for(session, user_id)
    if not bound.visible:
        return []
    result = await session.execute(
        bound.documents().order_by(Document.created_at.desc()).limit(limit).offset(offset)
    )
    return result.scalars().all()


async def get_document(
    session: AsyncSession, user_id: uuid.UUID, document_id: uuid.UUID
) -> Document | None:
    """A live document, or None if it does not exist *or* is not the caller's."""
    bound = await scope_for(session, user_id)
    if not bound.visible:
        return None
    result = await session.execute(bound.documents().where(Document.id == document_id))
    return result.scalar_one_or_none()


async def list_source_files(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> Sequence[SourceFile]:
    bound = await scope_for(session, user_id)
    if not bound.visible:
        return []
    result = await session.execute(
        bound.source_files()
        .order_by(SourceFile.received_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


async def get_source_file_by_hash(
    session: AsyncSession, sha256: str, library_id: uuid.UUID
) -> SourceFile | None:
    """Find an identical file **within one library**.

    Scoped, and it has to be. The lookup was global, matching the global unique
    constraint on `sha256`, and that was correct while there was one account.
    With several it is a cross-tenant leak in two directions at once: uploading
    a file would tell you whether another household already had it, hand back
    *their* filename and library id in the response, and silently not put your
    copy in your own library.

    The blob on disk is still shared — storage stays content-addressed, and one
    copy of identical bytes is the point of it. What is per-library is the
    *record* of holding them.
    """
    result = await session.execute(
        sa.select(SourceFile).where(
            SourceFile.sha256 == sha256, SourceFile.library_id == library_id
        )
    )
    return result.scalar_one_or_none()


async def get_source_file(
    session: AsyncSession, user_id: uuid.UUID, source_file_id: uuid.UUID
) -> SourceFile | None:
    """A source file, or None if it does not exist *or* is not the caller's.

    Deliberately one answer for both cases: whether a document exists in a
    library you cannot see is itself information.
    """
    bound = await scope_for(session, user_id)
    if not bound.visible:
        return None
    result = await session.execute(
        bound.source_files().where(SourceFile.id == source_file_id)
    )
    return result.scalar_one_or_none()


async def get_page(
    session: AsyncSession, user_id: uuid.UUID, source_file_id: uuid.UUID, page_number: int
) -> Page | None:
    bound = await scope_for(session, user_id)
    if not bound.visible:
        return None
    result = await session.execute(
        bound.pages().where(
            Page.source_file_id == source_file_id, Page.page_number == page_number
        )
    )
    return result.scalar_one_or_none()


async def list_pages(
    session: AsyncSession, user_id: uuid.UUID, source_file_id: uuid.UUID
) -> Sequence[Page]:
    """Every page of a file, as entities — `text` and its tsvector included.

    Only for the callers that read the text. Anything rendering a page list
    wants `list_pages_in_range`, which is three columns.
    """
    bound = await scope_for(session, user_id)
    if not bound.visible:
        return []
    result = await session.execute(
        bound.pages().where(Page.source_file_id == source_file_id).order_by(Page.page_number)
    )
    return result.scalars().all()


async def list_page_summaries(
    session: AsyncSession, user_id: uuid.UUID, source_file_id: uuid.UUID
) -> Sequence[sa.Row]:
    """Every page of a file, as the three scalars a page list draws.

    `list_pages_in_range`'s whole-file sibling. The file detail screen renders
    `PageOut` — `{page_number, render_path, thumb_path}` — and was loading the
    entity for it, so opening a 300-page bundle's detail view carried that
    bundle's entire OCR text and its persisted `text_tsv` to build a thumbnail
    strip. Same defect as the document view had, one route along.
    """
    bound = await scope_for(session, user_id)
    if not bound.visible:
        return []
    result = await session.execute(
        sa.select(Page.page_number, Page.render_path, Page.thumb_path)
        .where(bound.only(Page), Page.source_file_id == source_file_id)
        .order_by(Page.page_number)
    )
    return result.all()


async def list_page_text(
    session: AsyncSession, user_id: uuid.UUID, source_file_id: uuid.UUID
) -> Sequence[sa.Row]:
    """Every page's text, without the tsvector beside it.

    `GET /files/{id}/text` needs `text` and genuinely cannot avoid it — but it
    has no use for `text_tsv`, which is roughly the same size again and was
    coming along for the ride on every read of a large bundle.
    """
    bound = await scope_for(session, user_id)
    if not bound.visible:
        return []
    result = await session.execute(
        sa.select(Page.page_number, Page.text)
        .where(bound.only(Page), Page.source_file_id == source_file_id)
        .order_by(Page.page_number)
    )
    return result.all()


async def list_pages_in_range(
    session: AsyncSession,
    user_id: uuid.UUID,
    source_file_id: uuid.UUID,
    page_start: int,
    page_end: int,
) -> Sequence[sa.Row]:
    """The pages of one document's range, as the three scalars a viewer draws.

    Opening a document used to load every page of its *file* as a `Page` entity
    and then drop the ones outside the range in Python. A DD-214 is two pages
    inside a 300-page service-records bundle, so that read the whole bundle's
    OCR text and its persisted `text_tsv` — megabytes across the wire, ~99% of
    it discarded — on the one request the product is measured by.

    Two changes, and both matter: the columns are named (`PageOut` is
    `{page_number, render_path, thumb_path}` and nothing else), and the range is
    a SQL `BETWEEN` so the page index can skip the rest of the bundle.
    """
    bound = await scope_for(session, user_id)
    if not bound.visible:
        return []
    result = await session.execute(
        sa.select(Page.page_number, Page.render_path, Page.thumb_path)
        .where(
            bound.only(Page),
            Page.source_file_id == source_file_id,
            Page.page_number.between(page_start, page_end),
        )
        .order_by(Page.page_number)
    )
    return result.all()


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


def visible_job_ids(library_ids: list[uuid.UUID]):
    """The ids of `visible_jobs`, for use as a subquery inside an aggregate.

    Derived from `visible_jobs` rather than restating its joins. Those joins
    are subtle — a job reaches a library through *either* key, and the outer
    joins are the reason a classify failure is reachable at all — and a second
    copy would drift from this one silently, which is exactly how the pipeline
    screen went blind to classification failures the first time.
    """
    return visible_jobs(library_ids).with_only_columns(Job.id)
