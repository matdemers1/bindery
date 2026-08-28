"""Search (Architecture L7).

> The unit of search is the **page**, not the document.

Body text is matched with Postgres full-text search over `page.text_tsv` — GIN
indexed, page-granular — so a hit is always "page 47 of Army Records 2019.pdf"
and never just "somewhere in this 100-page file" (REQ-020).

Page hits are then rolled up **to documents** (ADR-001): one row per document,
its best-ranked page surfaced, and a count of how many of its pages matched. In
a 100-page bundle cut into thirty documents, that is what turns a hit into "the
DD-214" rather than "the bundle containing it".

**Known forms are boosted** (REQ-040). A registry match is a fact, so a document
Bindery *knows* is a DD-214 outranks a continuation sheet that merely mentions
the phrase.

Trigram fuzzy matching (REQ-023) covers *names*, which Postgres FTS is bad at —
the "Hoda" → "Honda" path. Correspondent and asset names join the same query
when those tables arrive in Phase 5.

Scoping is not optional and not a parameter a caller can forget: every query in
this module takes the caller's visible library set and filters on it.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.enums import SourceFileState
from api.db.models import Document, KnownForm, Page, SourceFile, Tag

# ts_headline settings: two short fragments, marked up for the results list.
HEADLINE_OPTIONS = (
    "StartSel=<mark>, StopSel=</mark>, MaxFragments=2, MaxWords=18, MinWords=6, "
    "FragmentDelimiter= … "
)
# Below this, a trigram "did you mean" is noise rather than help.
SUGGESTION_THRESHOLD = 0.3
MAX_SUGGESTIONS = 5

# Rank bonuses. ts_rank_cd on a normal hit lands well under 1, so a document the
# registry identifies as the thing being searched for leads decisively, while the
# general bonus for *being* a recognised form only breaks ties (REQ-040).
NAMED_FORM_BOOST = 1.0
ANY_FORM_BOOST = 0.05


@dataclass
class SearchFilters:
    library_ids: list[uuid.UUID] = field(default_factory=list)
    received_from: date | None = None
    received_to: date | None = None
    states: list[SourceFileState] = field(default_factory=list)
    known_form_codes: list[str] = field(default_factory=list)
    # Restrict to one file — the "search inside this bundle" path (REQ-118).
    source_file_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PageHit:
    page_number: int
    document_page_number: int
    snippet: str
    rank: float
    thumb_path: str | None
    render_path: str | None


@dataclass(frozen=True)
class SearchResult:
    document_id: uuid.UUID
    source_file_id: uuid.UUID
    library_id: uuid.UUID
    title: str | None
    original_filename: str | None
    page_start: int
    page_end: int
    file_page_count: int | None
    state: SourceFileState
    received_at: datetime
    known_form_code: str | None
    known_form_name: str | None
    best_page: PageHit
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


def _normalized(column):
    """Lowercase with separators stripped, so "DD-214" == "dd 214" == "dd214"."""
    return sa.func.replace(
        sa.func.replace(sa.func.lower(column), "-", ""), " ", ""
    )


def _form_names_the_query(query: str):
    """Does this query name a known form?

    Matched on the normalised code or the form's name — deliberately not a
    substring of the code, or a query of "dd" would claim every DD form.
    """
    normalized_query = query.strip().lower().replace("-", "").replace(" ", "")
    predicates = [_normalized(KnownForm.code) == normalized_query]
    if len(query.strip()) >= 3:
        predicates.append(KnownForm.name.op("ILIKE")(f"%{query.strip()}%"))
    return sa.and_(KnownForm.code.is_not(None), sa.or_(*predicates))


def _filters(filters: SearchFilters, allowed: list[uuid.UUID]) -> list:
    conditions = [
        Document.library_id.in_(allowed),
        Document.superseded_at.is_(None),
    ]
    if filters.received_from:
        conditions.append(SourceFile.received_at >= filters.received_from)
    if filters.received_to:
        conditions.append(SourceFile.received_at < filters.received_to)
    if filters.states:
        conditions.append(SourceFile.state.in_([state.value for state in filters.states]))
    if filters.source_file_id:
        conditions.append(SourceFile.id == filters.source_file_id)
    if filters.known_form_codes:
        conditions.append(KnownForm.code.in_(filters.known_form_codes))
    return conditions


def _columns(rank):
    """The shared shape of both match branches."""
    return [
        Page.id.label("page_id"),
        Page.page_number.label("page_number"),
        Page.render_path.label("render_path"),
        Page.thumb_path.label("thumb_path"),
        rank.label("rank"),
        Document.id.label("document_id"),
        Document.title.label("title"),
        Document.page_start.label("page_start"),
        Document.page_end.label("page_end"),
        Document.library_id.label("library_id"),
        SourceFile.id.label("source_file_id"),
        SourceFile.original_filename.label("original_filename"),
        SourceFile.page_count.label("file_page_count"),
        SourceFile.state.label("state"),
        SourceFile.received_at.label("received_at"),
        KnownForm.code.label("known_form_code"),
        KnownForm.name.label("known_form_name"),
    ]


def _joins(statement):
    """A document owns a page when the page falls inside its range.

    This is the join that makes "the DD-214" a result rather than "the bundle".
    """
    return (
        statement.join(SourceFile, SourceFile.id == Page.source_file_id)
        .join(
            Document,
            sa.and_(
                Document.source_file_id == Page.source_file_id,
                # Plain comparisons, deliberately. Phrasing this as int4range
                # containment to reach the GiST index the exclusion constraint
                # maintains was measurably *slower*: the planner reaches for
                # `uq_page_source_file_id_page_number` here, and that btree
                # already covers (source_file_id, page_number).
                Page.page_number >= Document.page_start,
                Page.page_number <= Document.page_end,
            ),
        )
        .outerjoin(KnownForm, KnownForm.id == Document.known_form_id)
    )


def _scoped_pages(query: str, visible: list[uuid.UUID], filters: SearchFilters):
    """Every match the caller may see, from two independent branches.

    **Text** — pages whose `tsvector` matches the query. Page-granular, GIN
    indexed, the primary path.

    **Known form** — documents the registry has *identified*, when the query
    names that form. This branch is why "find my DD-214" is a certainty rather
    than a ranked guess: OCR may have read the footer as "DD FORM 214", so a
    literal text search for "DD-214" would miss it entirely. Bindery already
    knows what the document is, and search should not have to rediscover it.

    Deliberately no `ts_headline` here. Generating a snippet means re-parsing the
    full page text, and this set can be tens of thousands of pages — the snippet
    is computed once the result page has been cut down to the rows actually
    being returned.
    """
    tsquery = _tsquery(query)
    allowed = filters.library_ids or visible
    # A caller cannot widen their scope by asking for a library they cannot see.
    allowed = [library_id for library_id in allowed if library_id in set(visible)]
    if not allowed:
        return None, allowed

    shared = _filters(filters, allowed)

    # A recognised document beats an unrecognised one on an otherwise equal hit.
    any_form_bonus = sa.case((KnownForm.code.is_not(None), ANY_FORM_BOOST), else_=0.0)
    text_matches = _joins(
        sa.select(*_columns(sa.func.ts_rank_cd(Page.text_tsv, tsquery) + any_form_bonus))
    ).where(sa.and_(Page.text_tsv.op("@@")(tsquery), *shared))

    # Anchored at the document's first page: that is where a reader expects a
    # form to open, regardless of which page carried the fingerprint.
    form_matches = _joins(
        sa.select(*_columns(sa.literal(NAMED_FORM_BOOST)))
    ).where(
        sa.and_(
            Page.page_number == Document.page_start,
            _form_names_the_query(query),
            *shared,
        )
    )

    return sa.union_all(text_matches, form_matches), allowed


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
    if pages is None:
        return SearchResponse(query=query, total=0, results=[], facets={}, suggestions=[])
    matches = pages.subquery("matches")

    # One row per document: its best page, and how many of its pages matched.
    best = sa.select(
        matches,
        sa.func.count().over(partition_by=matches.c.document_id).label("matching_pages"),
        sa.func.row_number()
        .over(
            partition_by=matches.c.document_id,
            order_by=(matches.c.rank.desc(), matches.c.page_number),
        )
        .label("row_number"),
    ).subquery("best")
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
            document_id=row.document_id,
            source_file_id=row.source_file_id,
            library_id=row.library_id,
            title=row.title,
            original_filename=row.original_filename,
            page_start=row.page_start,
            page_end=row.page_end,
            file_page_count=row.file_page_count,
            state=SourceFileState(row.state),
            received_at=row.received_at,
            known_form_code=row.known_form_code,
            known_form_name=row.known_form_name,
            best_page=PageHit(
                page_number=row.page_number,
                document_page_number=row.page_number - row.page_start + 1,
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
    suggestions = await suggest(session, query, allowed) if total == 0 else []

    return SearchResponse(
        query=query, total=total, results=results, facets=facets, suggestions=suggestions
    )


async def _facets(session: AsyncSession, rolled) -> dict[str, list[Facet]]:
    """Counts computed over the *matched* set, so each facet narrows honestly."""

    async def counts(column, label_column=None):
        selected = [column, sa.func.count()] if label_column is None else [
            column, label_column, sa.func.count()
        ]
        group = [column] if label_column is None else [column, label_column]
        return (
            await session.execute(
                sa.select(*selected)
                .where(column.is_not(None))
                .group_by(*group)
                .order_by(sa.func.count().desc())
            )
        ).all()

    library_rows = await counts(rolled.c.library_id)
    state_rows = await counts(rolled.c.state)
    form_rows = await counts(rolled.c.known_form_code, rolled.c.known_form_name)

    return {
        "library": [
            Facet(value=str(value), label=str(value), count=count)
            for value, count in library_rows
        ],
        "state": [
            Facet(value=str(value), label=str(value).replace("_", " "), count=count)
            for value, count in state_rows
        ],
        "known_form": [
            Facet(value=code, label=name or code, count=count)
            for code, name, count in form_rows
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
    titles = sa.select(
        Document.title.label("name"), similarity(Document.title, query).label("score")
    ).where(
        Document.title.is_not(None),
        Document.library_id.in_(allowed),
        Document.superseded_at.is_(None),
        similarity(Document.title, query) > SUGGESTION_THRESHOLD,
    )
    tags = sa.select(
        Tag.name.label("name"), similarity(Tag.name, query).label("score")
    ).where(
        sa.or_(Tag.library_id.in_(allowed), Tag.library_id.is_(None)),
        similarity(Tag.name, query) > SUGGESTION_THRESHOLD,
    )
    forms = sa.select(
        KnownForm.name.label("name"), similarity(KnownForm.code, query).label("score")
    ).where(
        KnownForm.enabled.is_(True),
        similarity(KnownForm.code, query) > SUGGESTION_THRESHOLD,
    )
    union = titles.union_all(tags, forms).subquery("candidates")

    rows = (
        await session.execute(
            sa.select(union.c.name).order_by(union.c.score.desc()).limit(MAX_SUGGESTIONS)
        )
    ).scalars().all()

    seen: list[str] = []
    for name in rows:
        if name not in seen:
            seen.append(name)
    return seen
