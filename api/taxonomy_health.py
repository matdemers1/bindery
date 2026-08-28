"""Taxonomy health, near-duplicates, and find-similar (T-5.6, T-5.10, T-5.11).

> Surfacing a problem with no tool to fix it is worse than not surfacing it.

So this ships alongside merge, not before it. Every problem it names has a
button next to it.

The tripwire it exists to watch is **R-08**: more than 15% of tags used exactly
once after a thousand classified documents means the `existing_ids` contract is
not holding and the prompt needs tightening.
"""

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Correspondent, Document, DocumentTag, DuplicatePair, Tag
from api.segments import live

# Trigram similarity above which two names are probably the same thing.
NAME_SIMILARITY = 0.55
# R-08's tripwire.
ORPHAN_RATIO_ALARM = 0.15
# Cosine similarity above which two documents are probably two scans of one thing.
DUPLICATE_SIMILARITY = 0.93


@dataclass
class HealthReport:
    total_tags: int
    used_once: int
    unused: int
    orphan_ratio: float
    exceeds_alarm: bool
    near_duplicate_tags: list[dict]
    near_duplicate_correspondents: list[dict]


async def report(session: AsyncSession, library_ids: list[uuid.UUID]) -> HealthReport:
    usage = (
        sa.select(DocumentTag.tag_id, sa.func.count().label("uses"))
        .join(Document, Document.id == DocumentTag.document_id)
        .where(
            Document.library_id.in_(library_ids), live(), DocumentTag.removed_at.is_(None)
        )
        .group_by(DocumentTag.tag_id)
        .subquery()
    )
    rows = (
        await session.execute(
            sa.select(Tag.id, Tag.name, sa.func.coalesce(usage.c.uses, 0).label("uses"))
            .outerjoin(usage, usage.c.tag_id == Tag.id)
            .where(
                sa.or_(Tag.library_id.in_(library_ids), Tag.library_id.is_(None)),
                Tag.merged_at.is_(None),
            )
        )
    ).all()

    total = len(rows)
    used_once = sum(1 for row in rows if row.uses == 1)
    unused = sum(1 for row in rows if row.uses == 0)
    ratio = used_once / total if total else 0.0

    return HealthReport(
        total_tags=total,
        used_once=used_once,
        unused=unused,
        orphan_ratio=round(ratio, 3),
        # R-08: the taxonomy is drifting despite the existing_ids design.
        exceeds_alarm=total >= 20 and ratio > ORPHAN_RATIO_ALARM,
        near_duplicate_tags=await _near_duplicate_names(session, Tag, library_ids),
        near_duplicate_correspondents=await _near_duplicate_names(
            session, Correspondent, library_ids
        ),
    )


async def _near_duplicate_names(session: AsyncSession, model, library_ids) -> list[dict]:
    """Pairs whose names are similar enough to probably be one thing.

    Trigram similarity, self-joined on id < id so each pair appears once. This
    is the same mechanism that turns "Hoda" into "Honda" in search, pointed at
    the taxonomy instead of at a query.
    """
    a, b = sa.orm.aliased(model), sa.orm.aliased(model)
    similarity = sa.func.similarity(a.name, b.name)
    rows = (
        await session.execute(
            sa.select(a.id, a.name, b.id, b.name, similarity.label("similarity"))
            .join(b, sa.and_(a.id < b.id, similarity > NAME_SIMILARITY))
            .where(
                sa.or_(a.library_id.in_(library_ids), a.library_id.is_(None)),
                sa.or_(b.library_id.in_(library_ids), b.library_id.is_(None)),
                a.merged_at.is_(None), b.merged_at.is_(None),
            )
            .order_by(similarity.desc())
            .limit(50)
        )
    ).all()
    return [
        {
            "a_id": str(row[0]), "a_name": row[1],
            "b_id": str(row[2]), "b_name": row[3],
            "similarity": round(float(row.similarity), 3),
        }
        for row in rows
    ]


async def find_similar(
    session: AsyncSession, document_id: uuid.UUID, library_ids: list[uuid.UUID], limit: int = 10
) -> list[dict]:
    """Documents that look like this one (REQ-117). Reuses the classification embeddings."""
    document = await session.get(Document, document_id)
    if document is None or document.embedding is None:
        return []

    distance = Document.embedding.cosine_distance(document.embedding)
    rows = (
        await session.execute(
            sa.select(Document.id, Document.title, distance.label("distance"))
            .where(
                Document.id != document_id,
                Document.library_id.in_(library_ids),
                Document.embedding.is_not(None),
                live(),
            )
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    return [
        {"document_id": str(row.id), "title": row.title,
         "similarity": round(1.0 - float(row.distance), 3)}
        for row in rows
    ]


async def detect_duplicates(
    session: AsyncSession, library_id: uuid.UUID, *, threshold: float = DUPLICATE_SIMILARITY
) -> int:
    """Record near-duplicate pairs (REQ-081). Never resolves them.

    Two scans of one deed at different qualities are both worth keeping until a
    human decides otherwise, and nothing here deletes on its own.
    """
    documents = (
        await session.execute(
            sa.select(Document.id, Document.embedding).where(
                Document.library_id == library_id, Document.embedding.is_not(None), live()
            )
        )
    ).all()

    found = 0
    for document_id, embedding in documents:
        distance = Document.embedding.cosine_distance(embedding)
        neighbours = (
            await session.execute(
                sa.select(Document.id, distance.label("distance"))
                .where(
                    Document.id != document_id,
                    Document.library_id == library_id,
                    Document.embedding.is_not(None),
                    live(),
                )
                .order_by(distance)
                .limit(5)
            )
        ).all()

        for other_id, other_distance in neighbours:
            similarity = 1.0 - float(other_distance)
            if similarity < threshold:
                continue
            # The check constraint requires an ordered pair.
            a, b = sorted([document_id, other_id], key=str)
            exists = (
                await session.execute(
                    sa.select(DuplicatePair.id).where(
                        DuplicatePair.document_a_id == a, DuplicatePair.document_b_id == b
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(
                    DuplicatePair(
                        library_id=library_id, document_a_id=a, document_b_id=b,
                        similarity=round(similarity, 4),
                    )
                )
                found += 1
    await session.flush()
    return found
