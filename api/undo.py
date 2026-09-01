"""Generic undo, driven by the audit trail (REQ-067).

> Every automated action is undoable, with the undo affordance present at the
> moment of the action.

`audit_event.before` / `after` are what make this mechanical rather than bespoke:
undoing an action is restoring the `before` it recorded, and the undo is itself
recorded so it can be undone in turn. Nothing is deleted at any point.

Only fields the recording action actually touched are restored. An undo that
reset everything would quietly discard edits made since, which is a worse
failure than the one it is fixing.
"""

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import field_source
from api.audit import record
from api.db.enums import ActorType, ReviewState, Sensitivity
from api.db.models import AuditEvent, Document, DocumentTag

# Actions that can be walked back, and the document fields each may have set.
# `route_to_review` is deliberately absent: "this needs your attention" is not a
# decision, so there is nothing in it to walk back.
UNDOABLE = {
    "classify": ("title", "summary", "document_date", "correspondent_id",
                 "document_type_id", "review_state"),
    "rule_applied": ("document_type_id", "sensitivity"),
    "file": ("review_state",),
    "edit": ("title", "summary", "document_date", "correspondent_id",
             "document_type_id", "sensitivity", "review_state"),
}

_COERCE = {
    "review_state": ReviewState,
    "sensitivity": Sensitivity,
    "correspondent_id": lambda value: uuid.UUID(value) if value else None,
    "document_type_id": lambda value: uuid.UUID(value) if value else None,
    "document_date": lambda value: date.fromisoformat(value) if value else None,
}


class UndoError(ValueError):
    """This action cannot be walked back."""


async def already_undone(
    session: AsyncSession, document_id: uuid.UUID
) -> set[uuid.UUID]:
    """Event ids that a previous undo has already walked back.

    Every undo records which event it reversed, so this needs no extra state —
    but it does need to be consulted, or pressing undo twice reverses the same
    action twice and the chain never advances.
    """
    rows = (
        await session.execute(
            sa.select(AuditEvent.after).where(
                AuditEvent.entity_type == "document",
                AuditEvent.entity_id == document_id,
                AuditEvent.action == "undo",
            )
        )
    ).scalars().all()

    undone: set[uuid.UUID] = set()
    for payload in rows:
        raw = (payload or {}).get("undid_event")
        if raw:
            try:
                undone.add(uuid.UUID(raw))
            except ValueError:
                continue
    return undone


async def latest_undoable(
    session: AsyncSession, document_id: uuid.UUID
) -> AuditEvent | None:
    """The most recent decision that has not already been walked back."""
    undone = await already_undone(session, document_id)
    conditions = [
        AuditEvent.entity_type == "document",
        AuditEvent.entity_id == document_id,
        AuditEvent.action.in_(tuple(UNDOABLE)),
    ]
    if undone:
        conditions.append(AuditEvent.id.not_in(undone))

    return (
        await session.execute(
            sa.select(AuditEvent)
            .where(sa.and_(*conditions))
            # By sequence, not timestamp: several events can share a transaction.
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def undo_event(
    session: AsyncSession, event: AuditEvent, *, actor_id: uuid.UUID | None
) -> Document:
    if event.action not in UNDOABLE:
        raise UndoError(f"{event.action!r} is not an undoable action")

    document = await session.get(Document, event.entity_id)
    if document is None:
        raise UndoError("the document no longer exists")

    before = event.before or {}
    restored: dict[str, object] = {}
    current: dict[str, object] = {}

    for name in UNDOABLE[event.action]:
        if name not in before:
            continue
        value = before[name]
        coerce = _COERCE.get(name)
        current[name] = _as_json(getattr(document, name))
        setattr(document, name, coerce(value) if coerce and value is not None else value)
        restored[name] = value

    undo_event_row = await record(
        session,
        entity_type="document",
        entity_id=document.id,
        action="undo",
        actor_type=ActorType.HUMAN,
        actor_id=actor_id,
        before=current,
        after={"undid": event.action, "undid_event": str(event.id), "restored": restored},
    )
    await session.flush()

    if event.action == "classify":
        # Take back the tags the classifier applied. Only AI-sourced links: a
        # tag a human or a rule set is not the classifier's to withdraw. The
        # links are superseded, not deleted, so the record of what was applied
        # survives the undo.
        result = await session.execute(
            sa.update(DocumentTag)
            .where(
                DocumentTag.document_id == document.id,
                DocumentTag.source == ActorType.AI.value,
                DocumentTag.removed_at.is_(None),
            )
            .values(removed_at=sa.func.now(), removed_by_event_id=undo_event_row.id)
        )
        # An undone classification goes back to the queue, not to a
        # pre-classification limbo — that is what "returns to review" means.
        document.review_state = ReviewState.NEEDS_REVIEW
        restored["review_state"] = ReviewState.NEEDS_REVIEW.value
        undo_event_row.after = {
            **(undo_event_row.after or {}),
            "restored": restored,
            "tags_withdrawn": result.rowcount or 0,
        }

    if event.action == "edit":
        # Give the fields back, or the value stays frozen at something nobody
        # chose: held by a person whose decision has just been reversed, and so
        # still off limits to classification. Released rather than deleted, so
        # "somebody set this and then took it back" stays answerable.
        await field_source.release(
            session, document.id, list(restored), event_id=undo_event_row.id
        )
        # Tags the edit revoked come back, and tags it added go away again,
        # read from the manifest the edit recorded rather than inferred from
        # timestamps — inference would sweep up tags a *later* edit added.
        # Without this the removal stands, and the removal is exactly what
        # stops classification re-applying the tag, so the undo would silently
        # do half its job.
        after = event.after or {}
        if put_back := after.get("tag_ids_removed"):
            await session.execute(
                sa.update(DocumentTag)
                .where(
                    DocumentTag.document_id == document.id,
                    DocumentTag.tag_id.in_([uuid.UUID(t) for t in put_back]),
                )
                .values(removed_at=None, removed_by_event_id=None)
            )
        if take_away := after.get("tag_ids_added"):
            await session.execute(
                sa.update(DocumentTag)
                .where(
                    DocumentTag.document_id == document.id,
                    DocumentTag.tag_id.in_([uuid.UUID(t) for t in take_away]),
                    DocumentTag.removed_at.is_(None),
                )
                .values(removed_at=sa.func.now(), removed_by_event_id=undo_event_row.id)
            )

    await session.flush()
    return document


def _as_json(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, uuid.UUID | date):
        return str(value)
    return getattr(value, "value", str(value))
