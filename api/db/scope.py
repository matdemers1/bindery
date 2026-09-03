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

This module is the only place the boundary is *assembled*. `api/db/repository.py`
is the second entry point, not a second implementation: it resolves a `Scope`
and asks it, so the library filter and the vault filter are written once and
travel together. They have to: they were written separately, one was updated,
and five read paths went past the vault (`api/vault/boundary.py`), and the
photo wall never gained a vault clause at all because applying it was something
each author had to remember.

`only(model)` is the seam for the queries a `Scope` cannot build for you —
aggregates, facets, hand-shaped joins. It returns *both* halves as one
condition, so the way to get a filter is also the way to get all of it.

**`tests/test_boundary_guard.py` is what keeps this true.** It AST-walks `api/`
and fails on a call site that builds `Model.library_id.in_(...)` by hand, or
selects documents, files or pages without naming the boundary, unless that site
is named in its grandfathered list — a list that may only shrink. The claim this
docstring used to make (that `tests/test_permission_boundary.py` asserted it)
was not true, and while it was not true the seam eroded back into twenty-seven
hand-written clauses, which is where those five leaks came from.
"""

import uuid
from collections.abc import Sequence
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
from api.vault import boundary as vault

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

    # `None` only for `for_libraries`, below: a boundary over an explicit set of
    # ids has no person behind it. Nothing the vault decides depends on who is
    # asking (`api/vault/boundary.py`), so there is nothing to get wrong.
    user_id: uuid.UUID | None
    visible: tuple[uuid.UUID, ...]
    writable: tuple[uuid.UUID, ...]
    roles: dict[uuid.UUID, MembershipRole]
    # **Not an input to any filter, and never read here** (CR-084). A vaulted
    # document is hidden whether the vault is open or shut (ADR-012), so unlock
    # state cannot widen what this boundary returns — `api/vault/boundary.py`
    # no longer accepts it, which is what makes that structural rather than a
    # convention. It was resolved from the session store on every single
    # request to be handed to two parameters that discarded it; that lookup is
    # gone, and so is the field — a token narrowing used to restate that a
    # bearer token cannot reach the vault, which was true and said nothing,
    # because nothing read it. What keeps a token out of the vault is that the
    # boundary hides vaulted rows unconditionally.

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

    def _vault_clause(self):
        return vault.document_clause(self.user_id)

    def only(self, model) -> sa.ColumnElement[bool]:
        """This caller's whole boundary for one model, as a single condition.

        For the queries a `Scope` cannot build — a facet count, an aggregate, a
        hand-shaped join. `statement.where(scope.only(Document))` is the
        supported way to write one, and it exists so that the two halves of the
        boundary cannot be applied separately: asking for the library filter
        *is* asking for the vault filter, because they come back as one clause.

        That is the whole design. `/api/photos` had the library half and not the
        vault half, and `/api/pipeline/files` had the library half and not the
        vault half, and both authors believed they had filtered.
        """
        if model is Document:
            return sa.and_(Document.library_id.in_(self.visible), self._vault_clause())
        if model is SourceFile:
            return sa.and_(
                SourceFile.library_id.in_(self.visible),
                SourceFile.id.not_in(vault.hidden_source_file_ids(self.user_id)),
            )
        if model is Page:
            # Pages carry no library of their own; they inherit their file's,
            # and their file's vault state. A vaulted document's page text is
            # what search, snippets and facets read, so this is the condition
            # that decides whether a locked vault leaks.
            return Page.source_file_id.in_(
                sa.select(SourceFile.id).where(self.only(SourceFile))
            )
        if model not in LIBRARY_SCOPED:
            raise TypeError(f"{model.__name__} is not library-scoped")
        return model.library_id.in_(self.visible)

    def documents(self, *, include_superseded: bool = False):
        query = sa.select(Document).where(self.only(Document))
        return query if include_superseded else query.where(live())

    def source_files(self):
        """A file is hidden while any live document over it is vaulted.

        Files and documents are different rows, and a vaulted document's file
        would otherwise still be listable, downloadable and countable — the
        page images are in it.
        """
        return sa.select(SourceFile).where(self.only(SourceFile))

    def pages(self):
        return sa.select(Page).where(self.only(Page))

    def of(self, model):
        """Any library-scoped model, filtered. Raises rather than guessing."""
        if model not in LIBRARY_SCOPED:
            raise TypeError(f"{model.__name__} is not library-scoped")
        if model is Document:
            # Routed through `documents()` so the vault filter cannot be
            # bypassed by asking for the model generically.
            return self.documents()
        if model is SourceFile:
            return self.source_files()
        return sa.select(model).where(self.only(model))

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


def for_libraries(
    library_ids: Sequence[uuid.UUID], *, viewer: uuid.UUID | None = None
) -> Scope:
    """A read boundary over library ids somebody has already been granted.

    For the layers that are handed ids rather than a request — search, Q&A —
    so they can ask for the boundary instead of restating half of it. Search is
    the path where restating it costs most: it reads page *text*, so a missing
    clause surfaces as the actual words.

    Read-only, deliberately. `writable` is empty, so `require_write` refuses
    everything rather than inferring authority from a list of ids whose
    provenance this function cannot see.
    """
    return Scope(
        user_id=viewer,
        visible=tuple(library_ids),
        writable=(),
        roles={},
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
