"""Correspondents, aliases, assets, and merge (T-5.1 to T-5.6).

> Merging correspondents or tags rewrites link rows across potentially thousands
> of documents. It must be previewed before commit, executed in one transaction,
> recorded as **one** operation with an affected-row manifest, and undoable as a
> single action.

Two decisions make that possible:

**Merge tombstones, it does not delete.** The merged-away record keeps a pointer
to its survivor, so undo is a flag flip and any historical reference still
resolves to something rather than dangling.

**The preview is the merge with writes off**, the same arrangement as bulk edit
and the rules dry-run. A preview produced by different code is a preview that
can lie.
"""

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import record
from api.db.enums import ActorType
from api.db.models import (
    AuditEvent,
    Correspondent,
    CorrespondentAlias,
    Document,
    DocumentAsset,
    DocumentTag,
    DocumentType,
    Tag,
)
from api.segments import live


class MergeError(ValueError):
    """The merge cannot be performed as asked."""


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or "untitled"


@dataclass
class MergePreview:
    from_name: str
    into_name: str
    document_count: int
    alias_count: int
    documents: list[dict] = field(default_factory=list)


async def resolve_correspondent(
    session: AsyncSession, name: str, library_id: uuid.UUID
) -> Correspondent | None:
    """Find a correspondent by name **or by any of its aliases** (REQ-071).

    Alias resolution at classification time is what keeps merge rare. A
    tombstoned record resolves to its survivor rather than to itself.
    """
    slug = slugify(name)

    found = (
        await session.execute(
            sa.select(Correspondent).where(
                Correspondent.library_id == library_id, Correspondent.slug == slug
            )
        )
    ).scalar_one_or_none()

    if found is None:
        found = (
            await session.execute(
                sa.select(Correspondent)
                .join(CorrespondentAlias,
                      CorrespondentAlias.correspondent_id == Correspondent.id)
                .where(
                    Correspondent.library_id == library_id,
                    CorrespondentAlias.slug == slug,
                )
            )
        ).scalar_one_or_none()

    # Follow the tombstone to whatever absorbed it.
    while found is not None and found.merged_into_id is not None:
        found = await session.get(Correspondent, found.merged_into_id)
    return found


async def add_alias(
    session: AsyncSession, correspondent: Correspondent, alias: str
) -> CorrespondentAlias | None:
    slug = slugify(alias)
    if slug == correspondent.slug:
        return None
    existing = (
        await session.execute(
            sa.select(CorrespondentAlias).where(
                CorrespondentAlias.correspondent_id == correspondent.id,
                CorrespondentAlias.slug == slug,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    created = CorrespondentAlias(
        correspondent_id=correspondent.id, alias=alias.strip(), slug=slug
    )
    session.add(created)
    await session.flush()
    return created


async def preview_correspondent_merge(
    session: AsyncSession, source_id: uuid.UUID, target_id: uuid.UUID
) -> MergePreview:
    """What a merge would touch. Writes nothing."""
    source = await session.get(Correspondent, source_id)
    target = await session.get(Correspondent, target_id)
    if source is None or target is None:
        raise MergeError("one of those correspondents does not exist")
    if source.id == target.id:
        raise MergeError("cannot merge a correspondent into itself")
    if source.library_id != target.library_id:
        raise MergeError("correspondents are in different libraries")

    documents = (
        await session.execute(
            sa.select(Document.id, Document.title)
            .where(Document.correspondent_id == source_id, live())
            .order_by(Document.created_at.desc())
            .limit(200)
        )
    ).all()
    total = (
        await session.execute(
            sa.select(sa.func.count()).select_from(Document)
            .where(Document.correspondent_id == source_id, live())
        )
    ).scalar_one()
    aliases = (
        await session.execute(
            sa.select(sa.func.count()).select_from(CorrespondentAlias)
            .where(CorrespondentAlias.correspondent_id == source_id)
        )
    ).scalar_one()

    return MergePreview(
        from_name=source.name, into_name=target.name,
        document_count=total, alias_count=aliases,
        documents=[{"id": str(i), "title": t} for i, t in documents],
    )


async def merge_correspondents(
    session: AsyncSession, source_id: uuid.UUID, target_id: uuid.UUID,
    *, actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """Merge, in one transaction, recorded as one undoable operation."""
    # Validates: raises MergeError for a self-merge, a missing record, or a
    # cross-library pair. Called for that, not for the result.
    await preview_correspondent_merge(session, source_id, target_id)
    source = await session.get(Correspondent, source_id)

    moved = (
        await session.execute(
            sa.select(Document.id).where(Document.correspondent_id == source_id)
        )
    ).scalars().all()

    event = await record(
        session,
        entity_type="correspondent", entity_id=target_id, action="merge_correspondent",
        actor_type=ActorType.HUMAN, actor_id=actor_id,
        before={
            "source_id": str(source_id), "source_name": source.name,
            "document_ids": [str(d) for d in moved],
        },
        after={"target_id": str(target_id), "document_count": len(moved)},
    )
    await session.flush()

    await session.execute(
        sa.update(Document)
        .where(Document.correspondent_id == source_id)
        .values(correspondent_id=target_id)
    )
    await session.execute(
        sa.update(CorrespondentAlias)
        .where(CorrespondentAlias.correspondent_id == source_id)
        .values(correspondent_id=target_id)
    )
    # The old name becomes an alias of the survivor, so it still resolves.
    target = await session.get(Correspondent, target_id)
    await add_alias(session, target, source.name)

    source.merged_into_id = target_id
    source.merged_at = datetime.now(UTC)
    await session.flush()
    return event.id


async def merge_tags(
    session: AsyncSession, source_id: uuid.UUID, target_id: uuid.UUID,
    *, actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """Merge two tags, retroactively, as one undoable action (REQ-076)."""
    source = await session.get(Tag, source_id)
    target = await session.get(Tag, target_id)
    if source is None or target is None:
        raise MergeError("one of those tags does not exist")
    if source.id == target.id:
        raise MergeError("cannot merge a tag into itself")

    links = (
        await session.execute(
            sa.select(DocumentTag.document_id, DocumentTag.source).where(
                DocumentTag.tag_id == source_id, DocumentTag.removed_at.is_(None)
            )
        )
    ).all()

    event = await record(
        session,
        entity_type="tag", entity_id=target_id, action="merge_tag",
        actor_type=ActorType.HUMAN, actor_id=actor_id,
        before={
            "source_id": str(source_id), "source_name": source.name,
            "links": [
                {"document_id": str(d), "source": getattr(s, "value", str(s))}
                for d, s in links
            ],
        },
        after={"target_id": str(target_id), "link_count": len(links)},
    )
    await session.flush()

    # Every existing link to the target, *including removed ones*.
    #
    # This used to filter on `removed_at IS NULL`, which reads naturally and is
    # wrong: `document_tag`'s primary key is `(document_id, tag_id)`, so a
    # removed link still occupies that key. A document that once carried the
    # target tag and had it taken off would make this insert collide, and the
    # whole merge would fail on a `pk_document_tag` violation.
    #
    # It survived Phase 5 because it needs a tag to have been removed from a
    # document *and* another tag later merged into it — which is exactly what a
    # merge, an undo, and a second merge produce.
    existing = {
        row.document_id: row
        for row in (
            await session.execute(
                sa.select(DocumentTag).where(DocumentTag.tag_id == target_id)
            )
        ).scalars().all()
    }
    for document_id, link_source in links:
        held = existing.get(document_id)
        if held is None:
            link = DocumentTag(
                document_id=document_id, tag_id=target_id, source=link_source
            )
            session.add(link)
            existing[document_id] = link
        elif held.removed_at is not None:
            # Revive rather than insert. The merge is saying this document
            # should carry the target tag, and it once did.
            held.removed_at = None
            held.removed_by_event_id = None

    await session.execute(
        sa.update(DocumentTag)
        .where(DocumentTag.tag_id == source_id, DocumentTag.removed_at.is_(None))
        .values(removed_at=datetime.now(UTC), removed_by_event_id=event.id)
    )
    source.merged_into_id = target_id
    source.merged_at = datetime.now(UTC)
    await session.flush()
    return event.id


async def merge_document_types(
    session: AsyncSession, source_id: uuid.UUID, target_id: uuid.UUID,
    *, actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """Merge two document types, retroactively, as one undoable action.

    Simpler than the correspondent merge because a type has no aliases: a
    document points at exactly one, so the whole operation is a re-point plus a
    tombstone. The document ids are recorded in `before` so undo can put back
    exactly the rows that moved, rather than every document now on the target.
    """
    source = await session.get(DocumentType, source_id)
    target = await session.get(DocumentType, target_id)
    if source is None or target is None:
        raise MergeError("one of those document types does not exist")
    if source.id == target.id:
        raise MergeError("cannot merge a document type into itself")
    if source.library_id != target.library_id:
        raise MergeError("cannot merge document types across libraries")

    moved = (
        await session.execute(
            sa.select(Document.id).where(Document.document_type_id == source_id)
        )
    ).scalars().all()

    event = await record(
        session,
        entity_type="document_type", entity_id=target_id, action="merge_document_type",
        actor_type=ActorType.HUMAN, actor_id=actor_id,
        before={
            "source_id": str(source_id), "source_name": source.name,
            "document_ids": [str(d) for d in moved],
        },
        after={"target_id": str(target_id), "document_count": len(moved)},
    )
    await session.flush()

    await session.execute(
        sa.update(Document)
        .where(Document.document_type_id == source_id)
        .values(document_type_id=target_id)
    )
    source.merged_into_id = target_id
    source.merged_at = datetime.now(UTC)
    await session.flush()
    return event.id


async def undo_merge(
    session: AsyncSession, event_id: uuid.UUID, *, actor_id: uuid.UUID | None
) -> int:
    """Reverse a merge — both the record and every link row it moved."""
    event = await session.get(AuditEvent, event_id)
    if event is None or event.action not in (
        "merge_correspondent", "merge_tag", "merge_document_type"
    ):
        raise MergeError("that is not a merge")

    already = (
        await session.execute(
            sa.select(sa.func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "undo_merge",
                AuditEvent.after["undid_event"].astext == str(event_id),
            )
        )
    ).scalar_one()
    if already:
        raise MergeError("that merge has already been undone")

    before = event.before or {}
    source_id = uuid.UUID(before["source_id"])
    restored = 0

    if event.action == "merge_correspondent":
        document_ids = [uuid.UUID(d) for d in before.get("document_ids", [])]
        if document_ids:
            await session.execute(
                sa.update(Document)
                .where(Document.id.in_(document_ids))
                .values(correspondent_id=source_id)
            )
            restored = len(document_ids)
        source = await session.get(Correspondent, source_id)
        if source:
            source.merged_into_id = None
            source.merged_at = None
    elif event.action == "merge_document_type":
        # A document points at exactly one type, so undo is the same re-point
        # in reverse — and only for the ids the merge actually moved, not every
        # document sitting on the target now.
        document_ids = [uuid.UUID(d) for d in before.get("document_ids", [])]
        if document_ids:
            await session.execute(
                sa.update(Document)
                .where(Document.id.in_(document_ids))
                .values(document_type_id=source_id)
            )
            restored = len(document_ids)
        source = await session.get(DocumentType, source_id)
        if source:
            source.merged_into_id = None
            source.merged_at = None
    else:
        target_id = uuid.UUID((event.after or {})["target_id"])
        for link in before.get("links", []):
            document_id = uuid.UUID(link["document_id"])
            await session.execute(
                sa.update(DocumentTag)
                .where(DocumentTag.document_id == document_id, DocumentTag.tag_id == source_id)
                .values(removed_at=None, removed_by_event_id=None)
            )
            await session.execute(
                sa.update(DocumentTag)
                .where(DocumentTag.document_id == document_id, DocumentTag.tag_id == target_id)
                .values(removed_at=datetime.now(UTC))
            )
            restored += 1
        source = await session.get(Tag, source_id)
        if source:
            source.merged_into_id = None
            source.merged_at = None

    await record(
        session,
        entity_type=event.entity_type, entity_id=event.entity_id, action="undo_merge",
        actor_type=ActorType.HUMAN, actor_id=actor_id,
        after={"undid_event": str(event_id), "restored": restored},
    )
    await session.flush()
    return restored


# --------------------------------------------------------------------------
# Assets (T-5.3, T-5.4)
# --------------------------------------------------------------------------


async def attach_asset(
    session: AsyncSession, document_id: uuid.UUID, asset_id: uuid.UUID, source
) -> None:
    existing = (
        await session.execute(
            sa.select(DocumentAsset).where(
                DocumentAsset.document_id == document_id, DocumentAsset.asset_id == asset_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.removed_at = None
        return
    session.add(DocumentAsset(document_id=document_id, asset_id=asset_id, source=source))
    await session.flush()


async def asset_timeline(session: AsyncSession, asset_id: uuid.UUID) -> list[dict]:
    """Everything about one asset, in the order it happened (REQ-075).

    Ordered by the document's own date where it has one, falling back to when it
    arrived — a service receipt filed years late still belongs at its date, not
    at the day it was scanned.
    """
    from api.db.models import Correspondent as C
    from api.db.models import SourceFile

    rows = (
        await session.execute(
            sa.select(
                Document.id, Document.title, Document.document_date,
                Document.page_start, Document.page_end, Document.source_file_id,
                SourceFile.received_at, SourceFile.original_filename,
                C.name.label("correspondent"),
            )
            .join(SourceFile, SourceFile.id == Document.source_file_id)
            .join(DocumentAsset, DocumentAsset.document_id == Document.id)
            .outerjoin(C, C.id == Document.correspondent_id)
            .where(
                DocumentAsset.asset_id == asset_id,
                DocumentAsset.removed_at.is_(None),
                live(),
            )
            .order_by(
                sa.func.coalesce(
                    Document.document_date, sa.cast(SourceFile.received_at, sa.Date)
                ).desc()
            )
        )
    ).all()

    return [
        {
            "document_id": str(row.id),
            "title": row.title or row.original_filename,
            "date": (row.document_date or row.received_at.date()).isoformat(),
            "dated_precisely": row.document_date is not None,
            "correspondent": row.correspondent,
            "source_file_id": str(row.source_file_id),
            "page_start": row.page_start,
        }
        for row in rows
    ]
