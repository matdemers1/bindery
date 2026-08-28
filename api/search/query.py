"""Search (Architecture L7).

> The unit of search is the **page**, not the document.

Body text is matched with Postgres full-text search over `page.text_tsv` — GIN
indexed, page-granular — so a hit is always "page 47 of Army Records 2019.pdf"
and never just "somewhere in this 100-page file" (REQ-020).

Page hits are then rolled up: one row per source file, showing its best-ranked
page and how many others matched. That keeps a bundle from flooding the results
with thirty rows of itself while still pointing at the exact page.

Trigram fuzzy matching (REQ-023) covers *names*, which Postgres FTS is bad at —
it is the "Hoda" → "Honda" path. In this phase only `document.title` and
`tag.name` exist to match against; correspondent and asset names join the same
query when those tables arrive in Phase 5.

Scoping is not optional and not a parameter a caller can forget: every query in
this module takes the caller's visible library set and filters on it.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.enums import SourceFileState
from api.db.models import Document, Page, SourceFile, Tag

# ts_headline settings: two short fragments, marked up for the results list.
HEADLINE_OPTIONS = (
    "StartSel=<mark>, StopSel=</mark>, MaxFragments=2, MaxWords=18, MinWords=6, "
    "FragmentDelimiter= … "
)
# Below this, a trigram "did you mean" is noise rather than help.
SUGGESTION_THRESHOLD = 0.3
MAX_SUGGESTIONS = 5


@dataclass
class SearchFilters:
    library_ids: list[uuid.UUID] = field(default_factory=list)
    received_from: date | None = None
    received_to: date | None = None
    states: list[SourceFileState] = field(default_factory=list)
    # Restrict to one file — the "search inside this bundle" path (REQ-118).
    source_file_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PageHit:
    page_number: int
    snippet: str
    rank: float
    thumb_path: str | None
    render_path: str | None


@dataclass(frozen=True)
class SearchResult:
    source_file_id: uuid.UUID
    library_id: uuid.UUID
    original_filename: str | None
    sha256: str
    page_count: int | None
    state: SourceFileState
    received_at: datetime
    best_page: PageHit
    # How many pages in this file matched, so a bundle can say "and 4 more".
    matching_pages: int


@dataclass(frozen=True)
class Facet:
    value: str
    label: str
    count: int


@dataclass(frozen=True)
class SearchResponse:
    query: str
    total: int
    results: list[SearchResult]
    facets: dict[str, list[Facet]]
    suggestions: list[str]


def _tsquery(query: str):
    """`websearch_to_tsquery` so quoted phrases and `-exclusions` just work.

    Unlike `to_tsquery` it never raises on user input, which matters when the
    query box is wired to every keystroke.
    """
    return sa.func.websearch_to_tsquery("english", query)


def _scoped_pages(query: str, visible: list[uuid.UUID], filters: SearchFilters):
    """Every matching page the caller is allowed to see, ranked but not rendered.

    Deliberately no `ts_headline` here. Generating a snippet means re-parsing the
    full page text, and this set can be tens of thousands of pages — the snippet
    is computed once the result page has been cut down to the rows actually
    being returned.
    """
    tsquery = _tsquery(query)
    allowed = filters.library_ids or visible
    # A caller cannot widen their scope by asking for a library they cannot see.
    allowed = [library_id for library_id in allowed if library_id in set(visible)]

    conditions = [
        Page.text_tsv.op("@@")(tsquery),
        SourceFile.library_id.in_(allowed),
    ]
    if filters.received_from:
        conditions.append(SourceFile.received_at >= filters.received_from)
    if filters.received_to:
        conditions.append(SourceFile.received_at < filters.received_to)
    if filters.states:
        conditions.append(SourceFile.state.in_([state.value for state in filters.states]))
    if filters.source_file_id:
        conditions.append(SourceFile.id == filters.source_file_id)

    return (
        sa.select(
            Page.id.label("page_id"),
            Page.source_file_id.label("source_file_id"),
            Page.page_number.label("page_number"),
            Page.render_path.label("render_path"),
            Page.thumb_path.label("thumb_path"),
            sa.func.ts_rank_cd(Page.text_tsv, tsquery).label("rank"),
            SourceFile.library_id.label("library_id"),
            SourceFile.original_filename.label("original_filename"),
            SourceFile.sha256.label("sha256"),
            SourceFile.page_count.label("page_count"),
            SourceFile.state.label("state"),
            SourceFile.received_at.label("received_at"),
        )
        .join(SourceFile, SourceFile.id == Page.source_file_id)
        .where(sa.and_(*conditions)),
        allowed,
    )


async def search(
    session: AsyncSession,
    query: str,
    visible_library_ids: list[uuid.UUID],
    *,
    filters: SearchFilters | None = None,
    limit: int = 25,
    offset: int = 0,
) -> SearchResponse:
    filters = filters or SearchFilters()
    query = query.strip()
    if not query or not visible_library_ids:
        return SearchResponse(query=query, total=0, results=[], facets={}, suggestions=[])

    pages, allowed = _scoped_pages(query, visible_library_ids, filters)
    if not allowed:
        return SearchResponse(query=query, total=0, results=[], facets={}, suggestions=[])
    matches = pages.subquery("matches")

    # One row per file: its best page, and how many pages matched in total.
    best = (
        sa.select(
            matches,
            sa.func.count().over(partition_by=matches.c.source_file_id).label("matching_pages"),
            sa.func.row_number()
            .over(
                partition_by=matches.c.source_file_id,
                order_by=(matches.c.rank.desc(), matches.c.page_number),
            )
            .label("row_number"),
        )
        .subquery("best")
    )
    rolled = sa.select(best).where(best.c.row_number == 1).subquery("rolled")

    total = (
        await session.execute(sa.select(sa.func.count()).select_from(rolled))
    ).scalar_one()

    # Cut to the result page *first*, then render snippets for those rows only.
    # This is the difference between one ts_headline call per returned result and
    # one per matching page in the archive.
    page_of_results = (
        sa.select(rolled)
        .order_by(rolled.c.rank.desc(), rolled.c.received_at.desc())
        .limit(limit)
        .offset(offset)
        .subquery("page_of_results")
    )
    rows = (
        await session.execute(
            sa.select(
                page_of_results,
                sa.func.ts_headline(
                    "english",
                    sa.func.coalesce(Page.text, ""),
                    _tsquery(query),
                    HEADLINE_OPTIONS,
                ).label("snippet"),
            )
            .join(Page, Page.id == page_of_results.c.page_id)
            .order_by(page_of_results.c.rank.desc(), page_of_results.c.received_at.desc())
        )
    ).all()

    results = [
        SearchResult(
            source_file_id=row.source_file_id,
            library_id=row.library_id,
            original_filename=row.original_filename,
            sha256=row.sha256,
            page_count=row.page_count,
            state=SourceFileState(row.state),
            received_at=row.received_at,
            best_page=PageHit(
                page_number=row.page_number,
                snippet=row.snippet,
                rank=float(row.rank),
                thumb_path=row.thumb_path,
                render_path=row.render_path,
            ),
            matching_pages=row.matching_pages,
        )
        for row in rows
    ]

    facets = await _facets(session, rolled)
    # Only offer a correction when the query found nothing — otherwise it is a
    # distraction from results the user already has.
    suggestions = (
        await suggest(session, query, allowed) if total == 0 else []
    )

    return SearchResponse(
        query=query, total=total, results=results, facets=facets, suggestions=suggestions
    )


async def _facets(session: AsyncSession, rolled) -> dict[str, list[Facet]]:
    """Counts computed over the *matched* set, so each facet narrows honestly."""
    library_rows = (
        await session.execute(
            sa.select(rolled.c.library_id, sa.func.count())
            .group_by(rolled.c.library_id)
            .order_by(sa.func.count().desc())
        )
    ).all()
    state_rows = (
        await session.execute(
            sa.select(rolled.c.state, sa.func.count())
            .group_by(rolled.c.state)
            .order_by(sa.func.count().desc())
        )
    ).all()

    return {
        "library": [
            Facet(value=str(library_id), label=str(library_id), count=count)
            for library_id, count in library_rows
        ],
        "state": [
            Facet(value=str(state), label=str(state).replace("_", " "), count=count)
            for state, count in state_rows
        ],
    }


async def suggest(
    session: AsyncSession, query: str, allowed: list[uuid.UUID]
) -> list[str]:
    """Trigram "did you mean" over names (REQ-023).

    This is the layer that turns "Hoda" into "Honda" — Postgres FTS will not,
    because it matches lexemes, not near-misses. Correspondent and asset names
    join this union in Phase 5.
    """
    similarity = sa.func.similarity
    titles = (
        sa.select(
            Document.title.label("name"), similarity(Document.title, query).label("score")
        )
        .where(
            Document.title.is_not(None),
            Document.library_id.in_(allowed),
            similarity(Document.title, query) > SUGGESTION_THRESHOLD,
        )
    )
    tags = (
        sa.select(Tag.name.label("name"), similarity(Tag.name, query).label("score"))
        .where(
            sa.or_(Tag.library_id.in_(allowed), Tag.library_id.is_(None)),
            similarity(Tag.name, query) > SUGGESTION_THRESHOLD,
        )
    )
    union = titles.union_all(tags).subquery("candidates")

    rows = (
        await session.execute(
            sa.select(union.c.name)
            .order_by(union.c.score.desc())
            .limit(MAX_SUGGESTIONS)
        )
    ).scalars().all()

    seen: list[str] = []
    for name in rows:
        if name not in seen:
            seen.append(name)
    return seen
