"""Building the candidate taxonomy from embedding neighbours (REQ-047).

> Passing the entire taxonomy scales badly and gives the model no signal about
> *likelihood* — only existence.

So the document is embedded, its nearest already-classified neighbours are
retrieved via pgvector, and *their* tags, types and correspondents become a
ranked candidate list. A tag five similar documents already carry is a much
better suggestion than one that merely exists.

**Candidates are permission-filtered before the prompt** (REQ-048). The returned
ids are re-validated afterwards in `resolve.py` — filtering the input is not
enough on its own, because a model can echo back an id it was never given.

Cold start is handled by falling back to the library's full taxonomy, which is
small precisely when the neighbour path is unavailable.
"""

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import (
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    Tag,
    live_tag_links,
)
from worker.ai.provider import Candidate

NEIGHBOUR_COUNT = 15
MAX_CANDIDATES = 25
# Cosine distance; 1.0 is orthogonal. Beyond this a "neighbour" shares so little
# vocabulary that its tags are noise.
MAX_NEIGHBOUR_DISTANCE = 0.85


@dataclass(frozen=True)
class CandidateSet:
    correspondents: list[Candidate]
    document_types: list[Candidate]
    tags: list[Candidate]
    neighbour_ids: list[uuid.UUID]
    # The closest neighbour's cosine *similarity*, a structural signal at the gate.
    best_similarity: float | None
    used_neighbours: bool


async def _neighbours(
    session: AsyncSession, document: Document, library_ids: list[uuid.UUID]
) -> list[tuple[uuid.UUID, float]]:
    if document.embedding is None:
        return []
    distance = Document.embedding.cosine_distance(document.embedding)
    rows = (
        await session.execute(
            sa.select(Document.id, distance.label("distance"))
            .where(
                Document.id != document.id,
                Document.library_id.in_(library_ids),
                Document.superseded_at.is_(None),
                Document.embedding.is_not(None),
                # Only documents that have actually been catalogued: an
                # unclassified neighbour has no taxonomy to contribute.
                Document.review_state.in_(("filed", "needs_review")),
            )
            .order_by(distance)
            .limit(NEIGHBOUR_COUNT)
        )
    ).all()
    return [(row.id, float(row.distance)) for row in rows if row.distance <= MAX_NEIGHBOUR_DISTANCE]


async def _from_neighbours(
    session: AsyncSession, neighbour_ids: list[uuid.UUID]
) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    tag_rows = (
        await session.execute(
            sa.select(Tag.id, Tag.name, sa.func.count())
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id.in_(neighbour_ids), live_tag_links())
            .group_by(Tag.id, Tag.name)
            .order_by(sa.func.count().desc(), Tag.name)
            .limit(MAX_CANDIDATES)
        )
    ).all()
    type_rows = (
        await session.execute(
            sa.select(DocumentType.id, DocumentType.name, sa.func.count())
            .join(Document, Document.document_type_id == DocumentType.id)
            .where(Document.id.in_(neighbour_ids))
            .group_by(DocumentType.id, DocumentType.name)
            .order_by(sa.func.count().desc(), DocumentType.name)
            .limit(MAX_CANDIDATES)
        )
    ).all()
    correspondent_rows = (
        await session.execute(
            sa.select(Correspondent.id, Correspondent.name, sa.func.count())
            .join(Document, Document.correspondent_id == Correspondent.id)
            .where(Document.id.in_(neighbour_ids))
            .group_by(Correspondent.id, Correspondent.name)
            .order_by(sa.func.count().desc(), Correspondent.name)
            .limit(MAX_CANDIDATES)
        )
    ).all()

    to_candidates = lambda rows: [  # noqa: E731
        Candidate(id=str(row[0]), name=row[1], neighbour_count=row[2]) for row in rows
    ]
    return to_candidates(correspondent_rows), to_candidates(type_rows), to_candidates(tag_rows)


async def _full_taxonomy(
    session: AsyncSession, library_ids: list[uuid.UUID]
) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    """Cold-start fallback. Small archives can afford the whole list."""

    async def load(model) -> list[Candidate]:
        rows = (
            await session.execute(
                sa.select(model.id, model.name)
                .where(sa.or_(model.library_id.in_(library_ids), model.library_id.is_(None)))
                .order_by(model.name)
                .limit(MAX_CANDIDATES)
            )
        ).all()
        return [Candidate(id=str(row[0]), name=row[1]) for row in rows]

    return await load(Correspondent), await load(DocumentType), await load(Tag)


async def build(
    session: AsyncSession,
    document: Document,
    library_ids: list[uuid.UUID],
    *,
    use_neighbours: bool = True,
) -> CandidateSet:
    """The taxonomy to offer this document, ranked by how likely it is to fit.

    `use_neighbours=False` is for a document being classified from its images
    because OCR found nothing to read. Its embedding describes an empty page,
    so its nearest neighbours are every *other* page nothing could be read
    from — and the tags they carry are the ones a previous, failed pass
    invented for them. Reuse then pulls those straight back: the archive ended
    up with "Blank or Unreadable Scan" as its single largest document type, 94
    documents, and the vision pass was returning titles like "Unknown - Blank
    or Unreadable Scan - Aircraft Icon" — the model had seen the aircraft and
    still picked the junk type, because it was what it was offered.

    A failed run must not get to set the vocabulary for its own retry.
    """
    neighbours = (
        await _neighbours(session, document, library_ids) if use_neighbours else []
    )
    neighbour_ids = [neighbour_id for neighbour_id, _ in neighbours]
    best_similarity = 1.0 - neighbours[0][1] if neighbours else None

    if neighbour_ids:
        correspondents, types, tags = await _from_neighbours(session, neighbour_ids)
        if tags or types or correspondents:
            return CandidateSet(
                correspondents=correspondents,
                document_types=types,
                tags=tags,
                neighbour_ids=neighbour_ids,
                best_similarity=best_similarity,
                used_neighbours=True,
            )

    correspondents, types, tags = await _full_taxonomy(session, library_ids)
    return CandidateSet(
        correspondents=correspondents,
        document_types=types,
        tags=tags,
        neighbour_ids=neighbour_ids,
        best_similarity=best_similarity,
        used_neighbours=False,
    )
