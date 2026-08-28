"""Bulk edit as a single undoable operation (REQ-087, REQ-068).

> Correcting thousands of documents one at a time is not viable.

Two properties make this safe enough to point at a thousand documents:

**A dry run is the same code path.** The preview is produced by the apply
function with writes turned off, so a preview that disagrees with the outcome is
not possible.

**The whole operation undoes as one action.** A bulk edit records a single audit
event carrying a manifest of every document it touched and their prior values.
Undoing it walks that manifest — not a thousand separate undos, which is the
same as no undo at all when you notice the mistake on document nine hundred.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.db.enums import ActorType, ReviewState, Sensitivity, TagSource
from api.db.models import AuditEvent, Document, DocumentTag, Tag, live_tag_links
from api.segments import live
from api.text import slugify


class BulkError(ValueError):
    """The operation is not something this can do."""


@dataclass
class BulkChange:
    document_id: uuid.UUID
    title: str | None
    changes: dict[str, Any] = field(default_factory=dict)


@dataclass
class BulkResult:
    matched: int
    changes: list[BulkChange]
    operation_id: uuid.UUID | None = None


async def _resolve_tags(
    session: AsyncSession, names: list[str], library_id: uuid.UUID
) -> list[uuid.UUID]:

    ids = []
    for name in names:
        slug = slugify(name)
        tag = (
            await session.execute(
                sa.select(Tag).where(Tag.library_id == library_id, Tag.slug == slug)
            )
        ).scalar_one_or_none()
        if tag is None:
            tag = Tag(library_id=library_id, name=name.strip(), slug=slug)
            session.add(tag)
            await session.flush()
        ids.append(tag.id)
    return ids


async def apply(
    session: AsyncSession,
    document_ids: list[uuid.UUID],
    actions: dict[str, Any],
    library_ids: list[uuid.UUID],
    *,
    actor_id: uuid.UUID | None,
    dry_run: bool = True,
) -> BulkResult:
    """Apply `actions` to every document the caller can write.

    Scoping is applied here rather than trusted from the caller: a bulk edit is
    exactly where a stray id would do the most damage.
    """
    if not actions:
        raise BulkError("nothing to do")
    unknown = set(actions) - {"add_tags", "remove_tags", "set_sensitivity", "set_review_state"}
    if unknown:
        raise BulkError(f"unsupported bulk action(s): {', '.join(sorted(unknown))}")

    documents = (
        await session.execute(
            sa.select(Document).where(
                Document.id.in_(document_ids),
                Document.library_id.in_(library_ids),
                live(),
            )
        )
    ).scalars().all()

    changes: list[BulkChange] = []
    manifest: list[dict[str, Any]] = []

    for document in documents:
        entry = BulkChange(document_id=document.id, title=document.title)
        before: dict[str, Any] = {}

        if names := actions.get("add_tags"):
            existing = set(
                (
                    await session.execute(
                        sa.select(DocumentTag.tag_id).where(
                            DocumentTag.document_id == document.id, live_tag_links()
                        )
                    )
                ).scalars().all()
            )
            if not dry_run:
                for tag_id in await _resolve_tags(session, names, document.library_id):
                    if tag_id not in existing:
                        session.add(
                            DocumentTag(
                                document_id=document.id, tag_id=tag_id, source=TagSource.HUMAN
                            )
                        )
            entry.changes["add_tags"] = names

        if names := actions.get("remove_tags"):
            rows = (
                await session.execute(
                    sa.select(DocumentTag.tag_id)
                    .join(Tag, Tag.id == DocumentTag.tag_id)
                    .where(
                        DocumentTag.document_id == document.id,
                        Tag.name.in_(names),
                        live_tag_links(),
                    )
                )
            ).scalars().all()
            if rows:
                entry.changes["remove_tags"] = names
                before["removed_tag_ids"] = [str(tag_id) for tag_id in rows]
                if not dry_run:
                    await session.execute(
                        sa.update(DocumentTag)
                        .where(
                            DocumentTag.document_id == document.id,
                            DocumentTag.tag_id.in_(rows),
                        )
                        .values(removed_at=datetime.now(UTC))
                    )

        if value := actions.get("set_sensitivity"):
            before["sensitivity"] = document.sensitivity.value
            entry.changes["set_sensitivity"] = value
            if not dry_run:
                document.sensitivity = Sensitivity(value)

        if value := actions.get("set_review_state"):
            before["review_state"] = document.review_state.value
            entry.changes["set_review_state"] = value
            if not dry_run:
                document.review_state = ReviewState(value)

        if entry.changes:
            changes.append(entry)
            manifest.append({"document_id": str(document.id), "before": before})

    if dry_run:
        return BulkResult(matched=len(changes), changes=changes)

    # One event for the whole operation. Undo reads this manifest.
    event = await record(
        session,
        entity_type="bulk_operation",
        entity_id=uuid.uuid4(),
        action="bulk_edit",
        actor_type=ActorType.HUMAN,
        actor_id=actor_id,
        before={"manifest": manifest},
        after={"actions": actions, "document_count": len(changes)},
    )
    await session.flush()
    return BulkResult(matched=len(changes), changes=changes, operation_id=event.id)


async def undo(
    session: AsyncSession, event_id: uuid.UUID, *, actor_id: uuid.UUID | None
) -> int:
    """Reverse an entire bulk edit in one action."""
    event = await session.get(AuditEvent, event_id)
    if event is None or event.action != "bulk_edit":
        raise BulkError("that is not a bulk operation")

    already = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "bulk_undo",
                AuditEvent.after["undid_event"].astext == str(event_id),
            )
        )
    ).scalar_one()
    if already:
        raise BulkError("that operation has already been undone")

    restored = 0
    for entry in (event.before or {}).get("manifest", []):
        document = await session.get(Document, uuid.UUID(entry["document_id"]))
        if document is None:
            continue
        before = entry.get("before", {})
        if "sensitivity" in before:
            document.sensitivity = Sensitivity(before["sensitivity"])
        if "review_state" in before:
            document.review_state = ReviewState(before["review_state"])
        if tag_ids := before.get("removed_tag_ids"):
            await session.execute(
                sa.update(DocumentTag)
                .where(
                    DocumentTag.document_id == document.id,
                    DocumentTag.tag_id.in_([uuid.UUID(t) for t in tag_ids]),
                )
                .values(removed_at=None)
            )
        # Tags the operation added are withdrawn, not deleted.
        if added := (event.after or {}).get("actions", {}).get("add_tags"):
            await session.execute(
                sa.update(DocumentTag)
                .where(
                    DocumentTag.document_id == document.id,
                    DocumentTag.tag_id.in_(
                        sa.select(Tag.id).where(Tag.name.in_(added))
                    ),
                    DocumentTag.source == TagSource.HUMAN.value,
                )
                .values(removed_at=datetime.now(UTC))
            )
        restored += 1

    await record(
        session,
        entity_type="bulk_operation",
        entity_id=event.entity_id,
        action="bulk_undo",
        actor_type=ActorType.HUMAN,
        actor_id=actor_id,
        after={"undid_event": str(event_id), "document_count": restored},
    )
    await session.flush()
    return restored
