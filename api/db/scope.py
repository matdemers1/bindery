"""The caller's permission boundary, resolved once per request (T-7.2, REQ-101).

> The property that makes this safe is that it is boring.

Every query that can reach a document filters on the caller's visible library
set. One join, no per-document ACL rows, no grant matrix, no inheritance. See
ADR-005.

The point of this module is that **no call site implements its own check**.
Routers ask a `Scope` for "the documents this caller can see" and are handed a
query that is already constrained; there is no supported way to ask for an
unconstrained one. Scattering the check is how leaks happen — a permission bug
here is not an inconvenience, it is the disclosure of one household member's
medical history to another.

`tests/test_permission_boundary.py` asserts that routers do not build their own
library filters, so the seam cannot quietly erode back into 27 hand-written
`library_id.in_(...)` clauses.
"""

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.enums import MembershipRole
from api.db.models import (
    Asset,
    AuditEvent,
    Correspondent,
    Document,
    DocumentType,
    ImportSession,
    Membership,
    Page,
    Rule,
    SavedSearch,
    SourceFile,
    Tag,
)
from api.segments import live

WRITE_ROLES = (MembershipRole.OWNER, MembershipRole.CONTRIBUTOR)

# Every model whose rows belong to exactly one library. Audit events are scoped
# by looking their entity up in these, so a model missing from this list means
# its history is invisible rather than leaked — the safe direction to fail.
LIBRARY_SCOPED = (
    Document, SourceFile, Tag, Correspondent, DocumentType, Rule,
    Asset, SavedSearch, ImportSession,
)


@dataclass(frozen=True)
class Scope:
    """What one caller may see and change. Immutable for the life of a request."""

    user_id: uuid.UUID
    visible: tuple[uuid.UUID, ...]
    writable: tuple[uuid.UUID, ...]
    roles: dict[uuid.UUID, MembershipRole]

    # -- guards ------------------------------------------------------------

    def require_any(self) -> None:
        if not self.visible:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no visible libraries")

    def require_visible(self, library_id: uuid.UUID) -> None:
        """404, not 403.

        Telling someone "that exists but is not yours" confirms the document
        exists. A reader probing UUIDs should not be able to enumerate the
        household's archive by watching status codes.
        """
        if library_id not in self.visible:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    def require_write(self, library_id: uuid.UUID) -> None:
        if library_id not in self.visible:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        if library_id not in self.writable:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "you have read-only access to that library"
            )

    def role_in(self, library_id: uuid.UUID) -> MembershipRole | None:
        return self.roles.get(library_id)

    def is_owner(self, library_id: uuid.UUID) -> bool:
        return self.roles.get(library_id) == MembershipRole.OWNER

    # -- already-filtered queries -----------------------------------------

    def documents(self, *, include_superseded: bool = False):
        query = sa.select(Document).where(Document.library_id.in_(self.visible))
        return query if include_superseded else query.where(live())

    def source_files(self):
        return sa.select(SourceFile).where(SourceFile.library_id.in_(self.visible))

    def pages(self):
        """Pages carry no library of their own; they inherit their file's."""
        return (
            sa.select(Page)
            .join(SourceFile, SourceFile.id == Page.source_file_id)
            .where(SourceFile.library_id.in_(self.visible))
        )

    def of(self, model):
        """Any library-scoped model, filtered. Raises rather than guessing."""
        if model not in LIBRARY_SCOPED:
            raise TypeError(f"{model.__name__} is not library-scoped")
        return sa.select(model).where(model.library_id.in_(self.visible))

    def audit(self):
        """History, scoped by resolving each event's entity to a library.

        Audit rows are polymorphic — `entity_id` may be a document, a rule, a
        tag, or something with no library at all (an export, an integrity run).
        Rather than denormalising a `library_id` onto the table, where it could
        drift out of step with the row it describes, the boundary is resolved
        from the entity itself at read time.

        Events with no library-scoped entity are visible only to the person who
        caused them. That is deliberately strict: it is better for a household
        member to miss a system event than to see one from a library they have
        no membership in.
        """
        reachable = sa.union_all(
            *[
                sa.select(model.id.label("id")).where(model.library_id.in_(self.visible))
                for model in LIBRARY_SCOPED
            ]
        ).subquery()

        return sa.select(AuditEvent).where(
            sa.or_(
                AuditEvent.entity_id.in_(sa.select(reachable.c.id)),
                AuditEvent.actor_id == self.user_id,
            )
        )


async def resolve(session: AsyncSession, user_id: uuid.UUID) -> Scope:
    """Read the caller's memberships once, and answer every question from them."""
    rows = (
        await session.execute(
            sa.select(Membership.library_id, Membership.role).where(
                Membership.user_id == user_id
            )
        )
    ).all()

    roles = {row.library_id: row.role for row in rows}
    return Scope(
        user_id=user_id,
        visible=tuple(roles),
        writable=tuple(lid for lid, role in roles.items() if role in WRITE_ROLES),
        roles=roles,
    )
