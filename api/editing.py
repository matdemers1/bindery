"""Correcting a document by hand (T-17.2 to T-17.4, REQ-188 to REQ-190).

Bindery could find any document in the archive in ten seconds and could not fix
a single one of them. This is the other half of the governing principle: every
automated decision cheap to *inspect* shipped in Phase 3, and reversal shipped
as undo, but "reverse it" is not "fix it" — undo takes you back to nothing, it
does not get you to right.

Three things hold this together:

**Every change is one audited `edit` action.** `api/undo.py` has defined the
`edit` action and its field list since Phase 3 and nothing ever recorded one;
this is the route it was waiting for, so undo works without new machinery.

**Every changed field is claimed for the person.** `api/field_source` is what
stops the next classification run quietly putting the old value back.

**Taxonomy resolves by id, and creating is a separate, explicit act**
(invariant 6). A name never becomes a link by matching something that looks
like it — the caller either passes an id it already has, or says in as many
words that it wants a new one.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import field_source
from api.audit import record
from api.db.enums import ActorType, TagSource
from api.db.enums import FieldSource as Kind
from api.db.models import (
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    Tag,
)
from api.text import slugify

log = logging.getLogger("bindery.editing")

# The fields a PATCH may set. Same list `field_source.EDITABLE` can hold, and
# the same list `undo.UNDOABLE["edit"]` knows how to restore — they are three
# views of one decision and drift between them is a silent bug.
FIELDS = field_source.EDITABLE


class EditError(ValueError):
    """The edit cannot be applied, and nothing has been changed."""


@dataclass
class EditResult:
    document_id: uuid.UUID
    changed: dict[str, Any] = field(default_factory=dict)
    tags_added: list[str] = field(default_factory=list)
    tags_removed: list[str] = field(default_factory=list)
    created: dict[str, str] = field(default_factory=dict)
    event_id: uuid.UUID | None = None

    @property
    def touched(self) -> bool:
        return bool(self.changed or self.tags_added or self.tags_removed)


def _serialise(value: Any) -> Any:
    """Audit rows are JSON, and undo reads them back. `_COERCE` in `undo.py` is
    the other half of this — the two must agree about the wire form."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


async def _resolve_correspondent(
    session: AsyncSession, library_id: uuid.UUID, correspondent_id: uuid.UUID | None
) -> uuid.UUID | None:
    """By id, and by nothing else.

    Re-validated against the document's library rather than trusted: an id from
    another library would otherwise link a document to a correspondent its
    owner cannot see, which is the access boundary leaking sideways through a
    field nobody thinks of as a permission.
    """
    if correspondent_id is None:
        return None
    found = await session.get(Correspondent, correspondent_id)
    if found is None or found.library_id not in (None, library_id):
        raise EditError("that correspondent does not exist in this library")
    return found.id


async def _resolve_type(
    session: AsyncSession, library_id: uuid.UUID, type_id: uuid.UUID | None
) -> uuid.UUID | None:
    if type_id is None:
        return None
    found = await session.get(DocumentType, type_id)
    if found is None or found.library_id not in (None, library_id):
        raise EditError("that document type does not exist in this library")
    return found.id


async def create_correspondent(
    session: AsyncSession, library_id: uuid.UUID, name: str
) -> Correspondent:
    """Make a new one, deliberately, because the caller asked for exactly this.

    No fuzzy match against what already exists (invariant 6). If this makes a
    near-duplicate of one already in the library, Organise merges it — which is
    a cheap, visible, undoable correction, and much better than a name silently
    resolving to something the person did not pick.
    """
    name = name.strip()
    if not name:
        raise EditError("a correspondent needs a name")
    row = Correspondent(library_id=library_id, name=name, slug=slugify(name))
    session.add(row)
    await session.flush()
    return row


async def create_document_type(
    session: AsyncSession, library_id: uuid.UUID, name: str
) -> DocumentType:
    name = name.strip()
    if not name:
        raise EditError("a document type needs a name")
    row = DocumentType(library_id=library_id, name=name, slug=slugify(name))
    session.add(row)
    await session.flush()
    return row


async def create_tag(session: AsyncSession, library_id: uuid.UUID, name: str) -> Tag:
    name = name.strip()
    if not name:
        raise EditError("a tag needs a name")
    row = Tag(library_id=library_id, name=name, slug=slugify(name))
    session.add(row)
    await session.flush()
    return row


async def _apply_tags(
    session: AsyncSession,
    document: Document,
    add: list[uuid.UUID],
    remove: list[uuid.UUID],
    event_id: uuid.UUID,
) -> tuple[list[str], list[str], list[uuid.UUID], list[uuid.UUID]]:
    """Add and revoke links, recorded as a person's doing.

    Revoked, never deleted — the link row is the record that a tag was once
    applied and then taken back, and `removed_by_event_id` is what lets
    classification tell a person's removal from its own (T-17.6).
    """
    rows = (
        await session.execute(
            sa.select(DocumentTag).where(DocumentTag.document_id == document.id)
        )
    ).scalars().all()
    links = {row.tag_id: row for row in rows}

    added: list[str] = []
    actually_added: list[uuid.UUID] = []
    for tag_id in add:
        tag = await session.get(Tag, tag_id)
        if tag is None or tag.library_id not in (None, document.library_id):
            raise EditError("that tag does not exist in this library")
        existing = links.get(tag_id)
        if existing is not None and existing.removed_at is None:
            continue
        if existing is not None:
            existing.removed_at = None
            existing.removed_by_event_id = None
            existing.source = TagSource.HUMAN
        else:
            session.add(
                DocumentTag(
                    document_id=document.id, tag_id=tag_id, source=TagSource.HUMAN
                )
            )
        added.append(tag.name)
        actually_added.append(tag_id)

    removed: list[str] = []
    actually_removed: list[uuid.UUID] = []
    for tag_id in remove:
        existing = links.get(tag_id)
        if existing is None or existing.removed_at is not None:
            continue
        existing.removed_at = sa.func.now()
        existing.removed_by_event_id = event_id
        tag = await session.get(Tag, tag_id)
        removed.append(tag.name if tag else str(tag_id))
        actually_removed.append(tag_id)

    await session.flush()
    return added, removed, actually_added, actually_removed


async def apply(
    session: AsyncSession,
    document: Document,
    changes: dict[str, Any],
    *,
    actor_id: uuid.UUID,
    add_tags: list[uuid.UUID] | None = None,
    remove_tags: list[uuid.UUID] | None = None,
) -> EditResult:
    """Apply one person's corrections as a single undoable action.

    `changes` carries only the fields the caller actually sent — a field absent
    from the payload is untouched, which is what makes clearing a title
    (sending `null`) different from not mentioning it.
    """
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise EditError(f"not an editable field: {', '.join(sorted(unknown))}")

    if "correspondent_id" in changes:
        changes["correspondent_id"] = await _resolve_correspondent(
            session, document.library_id, changes["correspondent_id"]
        )
    if "document_type_id" in changes:
        changes["document_type_id"] = await _resolve_type(
            session, document.library_id, changes["document_type_id"]
        )

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for name, value in changes.items():
        current = getattr(document, name)
        if current == value:
            # Recording a no-op would make undo restore a value to itself and
            # claim the field for a person who did not actually decide anything.
            continue
        before[name] = _serialise(current)
        after[name] = _serialise(value)
        setattr(document, name, value)

    event = await record(
        session,
        entity_type="document",
        entity_id=document.id,
        action="edit",
        actor_type=ActorType.HUMAN,
        actor_id=actor_id,
        before=before or None,
        after=after or None,
    )
    await session.flush()

    added, removed, added_ids, removed_ids = await _apply_tags(
        session, document, add_tags or [], remove_tags or [], event.id
    )

    # A manifest of exactly which links this edit touched, so undo can put them
    # back without guessing. Inferring it from timestamps would sweep up tags a
    # *later* edit added, which is the kind of undo that quietly does more than
    # it was asked to.
    if added or removed:
        event.after = {
            **(event.after or {}),
            "tag_ids_added": [str(tag_id) for tag_id in added_ids],
            "tag_ids_removed": [str(tag_id) for tag_id in removed_ids],
        }

    # The claim is what makes the correction survive tonight's classification.
    await field_source.record(
        session,
        document.id,
        list(after),
        Kind.HUMAN,
        actor_id=actor_id,
        event_id=event.id,
    )

    log.info(
        "document %s edited by %s: %s",
        document.id, actor_id, ", ".join(sorted(after)) or "tags only",
    )
    return EditResult(
        document_id=document.id,
        changed=after,
        tags_added=added,
        tags_removed=removed,
        event_id=event.id,
    )
