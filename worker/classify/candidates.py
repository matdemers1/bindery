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

# The archive-wide fallback can afford a much longer list than the neighbour
# path, because it is identical for every document in a run and therefore sits
# inside the cached prompt prefix. Twenty-five was never a considered number for
# this path — it was the neighbour cap, reused.
MAX_FALLBACK_CANDIDATES = 150

# Usage bands, not exact counts. An exact count changes every time anything is
# classified, and the candidate block is the *cached* half of the prompt — a
# prefix that changes on every document is a prefix that is never read from
# cache. A band moves rarely, so the block stays byte-identical across a run
# while still telling the model which entries are common.
_BANDS = (
    (25, "used by 25+ documents"),
    (10, "used by 10+ documents"),
    (5, "used by 5+ documents"),
    (2, "used by 2+ documents"),
    (1, "used once"),
)


def _band(count: int) -> tuple[int, str]:
    """`(rank, label)` for a usage count. Rank sorts; label explains.

    Both are stable across a whole band, which is the point: a type going from
    six documents to seven must not change a single byte of the prompt.
    """
    for index, (floor, label) in enumerate(_BANDS):
        if count >= floor:
            return len(_BANDS) - index, label
    return 0, ""


def _banded(identifier, name: str, count: int) -> Candidate:
    return Candidate(
        id=str(identifier), name=name, usage_count=count, usage_label=_band(count)[1]
    )


def _stable_order(candidates: list[Candidate]) -> list[Candidate]:
    """Band descending, then name. Deliberately *not* by exact count.

    SQL picks which entries make the cut, by exact count, which is the right
    question for membership. Ordering them by exact count would then reshuffle
    the list every time anything was classified — and this list is the cached
    half of the prompt.
    """
    return sorted(candidates, key=lambda c: (-_band(c.usage_count)[0], c.name))
# Cosine distance; 1.0 is orthogonal. Beyond this a "neighbour" shares so little
# vocabulary that its tags are noise.
MAX_NEIGHBOUR_DISTANCE = 0.85

# Document types that describe a *failure to read* rather than a kind of
# document. They enter the taxonomy honestly — a text-only pass over a
# photograph really did have nothing to work from, and "Blank or Unreadable
# Scan" was a fair summary of an empty string — and then they persist, because
# reuse offers back whatever already exists.
#
# They are withheld only from a document being classified from its pictures,
# where offering them is self-contradictory: the premise of that request is
# "you can see this, tell me what it is", and the reply came back as
# "Unknown - Blank or Unreadable Scan - Bird Illustration". The model had seen
# the bird. It picked the type it was given.
UNREADABLE_TYPE_SIGNATURES = (
    "unreadable", "illegible", "blank scan", "blank or unreadable",
    "no extractable", "no legible", "not legible", "cannot be identified",
)


def _describes_a_failure_to_read(name: str) -> bool:
    lowered = name.lower()
    return any(signature in lowered for signature in UNREADABLE_TYPE_SIGNATURES)


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
        Candidate(id=str(row[0]), name=row[1], usage_count=row[2]) for row in rows
    ]
    return to_candidates(correspondent_rows), to_candidates(type_rows), to_candidates(tag_rows)


async def _full_taxonomy(
    session: AsyncSession, library_ids: list[uuid.UUID], *, readable: bool = True
) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    """The archive's own taxonomy, most-used first.

    This is the fallback whenever the neighbour path has nothing to say, which
    is cold start — and, since T-8.5, every document being classified from its
    pictures. It used to read `.order_by(name).limit(25)`, under a docstring
    saying "small archives can afford the whole list". That was true when it was
    written and stopped being true silently, because a LIMIT truncates rather
    than errors.

    By the time the archive held 277 document types, the model was being shown
    the first 25 **alphabetically** — a list ending at "Business Plan / Product
    Concept". `Utility Bill`, `Resume`, `Training Presentation` and
    `Purchase Order` were never offered, so they could not be reused, so the
    model invented near-duplicates; and every invention made the alphabetical
    window a smaller fraction of the whole. R-08's tripwire is 15% of tags used
    exactly once. It reached 68.1%.
    """
    live_document = sa.and_(Document.superseded_at.is_(None), Document.library_id.in_(library_ids))

    async def by_document_column(model, column) -> list[Candidate]:
        rows = (
            await session.execute(
                sa.select(model.id, model.name, sa.func.count(Document.id))
                .outerjoin(Document, sa.and_(column == model.id, live_document))
                .where(sa.or_(model.library_id.in_(library_ids), model.library_id.is_(None)))
                .group_by(model.id, model.name)
                .order_by(sa.func.count(Document.id).desc(), model.name)
                .limit(MAX_FALLBACK_CANDIDATES)
            )
        ).all()
        return _stable_order([_banded(row[0], row[1], row[2]) for row in rows])

    tag_rows = (
        await session.execute(
            sa.select(Tag.id, Tag.name, sa.func.count(DocumentTag.document_id))
            .outerjoin(
                DocumentTag, sa.and_(DocumentTag.tag_id == Tag.id, live_tag_links())
            )
            .where(sa.or_(Tag.library_id.in_(library_ids), Tag.library_id.is_(None)))
            .group_by(Tag.id, Tag.name)
            .order_by(sa.func.count(DocumentTag.document_id).desc(), Tag.name)
            .limit(MAX_FALLBACK_CANDIDATES)
        )
    ).all()

    correspondents = await by_document_column(Correspondent, Document.correspondent_id)
    types = await by_document_column(DocumentType, Document.document_type_id)
    tags = _stable_order([_banded(row[0], row[1], row[2]) for row in tag_rows])

    if not readable:
        types = [t for t in types if not _describes_a_failure_to_read(t.name)]
    return correspondents, types, tags


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

    It also withholds the document types that describe a failure to read —
    see `UNREADABLE_TYPE_SIGNATURES`. Skipping the neighbours alone was not
    enough: the fallback is the library's whole taxonomy, and "Blank or
    Unreadable Scan" is in it.

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

    correspondents, types, tags = await _full_taxonomy(
        session, library_ids, readable=use_neighbours
    )
    return CandidateSet(
        correspondents=correspondents,
        document_types=types,
        tags=tags,
        neighbour_ids=neighbour_ids,
        best_similarity=best_similarity,
        used_neighbours=False,
    )
