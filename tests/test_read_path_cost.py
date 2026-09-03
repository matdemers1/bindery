"""What the read paths cost, asserted rather than remembered (round 7).

Every fix in here was made once already, in spirit, and decayed — the facets
were collapsed and nothing counted the statements afterwards; the word-box
cache was added and nothing checked it kept the one artifact it was written
for. So this file measures rather than describes: statements are counted off
the engine, and the indexes are proved by asking the planner to use them rather
than by asserting a row in `pg_indexes`, which says an index exists and nothing
about whether any query can reach it.

`enable_seqscan = off` is how the index assertions stay honest on a test-sized
table. It does not force a bad plan into existence — the planner still refuses
an index that cannot answer the predicate — so a plan that names the index is
proof the expression matches, which is the part that silently breaks.
"""

import contextlib
import json
import re
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from api.artifacts import derived_for
from api.db.enums import IngestSource, SourceFileState, TagSource
from api.db.models import Correspondent, Document, DocumentTag, Page, SourceFile, Tag
from api.db.session import engine
from api.routers import files as files_router
from api.routers import library as library_router
from api.search import query as search_query

# `FROM document` and not `FROM document_tag`: the word boundary matters, and it
# is the difference between counting the aggregate scans this file is about and
# counting the tag join, which was never the problem.
TOUCHES_DOCUMENT = re.compile(r"\bFROM document\b", re.IGNORECASE)


@contextlib.contextmanager
def capture_statements() -> list[str]:
    """Every statement the shared engine executes inside the block."""
    seen: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def _index_is_reachable(session, statement: str, index: str) -> str:
    """The plan for `statement` with sequential scans priced out of the way."""
    await session.execute(sa.text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        row[0] for row in (await session.execute(sa.text(f"EXPLAIN {statement}"))).all()
    )
    await session.execute(sa.text("SET LOCAL enable_seqscan = on"))
    assert index in plan, f"{index} cannot serve this predicate:\n{plan}"
    return plan


# ---------------------------------------------------------------------------
# CR-102 — the palette and Ask paid for three facet aggregations they discard
# ---------------------------------------------------------------------------


@pytest.fixture
async def a_searchable_document(session, signed_in):
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id,
        sha256=uuid.uuid4().hex * 2,
        byte_size=1024,
        original_filename="Discharge.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    session.add(
        Page(
            source_file_id=source_file.id,
            page_number=1,
            text="certificate of release or discharge from active duty",
        )
    )
    session.add(
        Document(
            library_id=library.id,
            source_file_id=source_file.id,
            page_start=1,
            page_end=1,
            title="Discharge",
        )
    )
    await session.commit()
    # `page.text_tsv` is a generated column; the ORM never wrote it, so read the
    # row back rather than trusting the flush.
    return library


async def test_facets_false_skips_the_aggregation_and_still_counts(
    session, a_searchable_document
) -> None:
    """The ⌘K palette renders seven titles inside a 100 ms budget and throws the
    library, state and known-form breakdowns away. It should not be paying for
    them — and it must still get an exact total, because the total is what
    decides whether a spelling suggestion is offered."""
    library = a_searchable_document

    with capture_statements() as with_facets:
        full = await search_query.search(session, "discharge", [library.id])
    with capture_statements() as without_facets:
        lean = await search_query.search(
            session, "discharge", [library.id], facets=False
        )

    assert full.total == lean.total == 1
    assert [r.document_id for r in full.results] == [r.document_id for r in lean.results]
    assert lean.facets == {}
    assert full.facets, "the default still returns facets"

    grouped = [s for s in with_facets if "GROUPING SETS" in s.upper()]
    lean_grouped = [s for s in without_facets if "GROUPING SETS" in s.upper()]
    assert len(grouped) == 1, "the four facet questions are one statement"
    assert lean_grouped == [], (
        "a caller that renders no facets still ran the grouping sets"
    )


# ---------------------------------------------------------------------------
# CR-103 — the bundle big enough to matter was the one the cache refused
# ---------------------------------------------------------------------------


@pytest.fixture
async def a_bundle_with_large_word_boxes(session, signed_in, monkeypatch):
    """A word-box artifact deliberately larger than the whole cache budget.

    Shrinking the budget rather than writing tens of megabytes: the condition
    under test is "one artifact exceeds the budget", and it is the ratio that
    matters, not the absolute size.
    """
    _, library = await signed_in()
    sha = uuid.uuid4().hex * 2
    source_file = SourceFile(
        library_id=library.id,
        sha256=sha,
        byte_size=4096,
        original_filename="Army Records 2019.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=4,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    for number in range(1, 5):
        session.add(Page(source_file_id=source_file.id, page_number=number, text="x"))
    session.add(
        Document(
            library_id=library.id,
            source_file_id=source_file.id,
            page_start=1,
            page_end=4,
            title="Army Records",
        )
    )
    await session.commit()

    paths = derived_for(sha)
    paths.mkdirs()
    paths.word_boxes.write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "number": number,
                        "words": [
                            {"t": "w" * 64, "x": 1, "y": 1, "w": 1, "h": 1}
                            for _ in range(200)
                        ],
                    }
                    for number in range(1, 5)
                ]
            }
        )
    )
    monkeypatch.setattr(files_router, "_BOXES_CACHE_BYTES", 1024)
    files_router._boxes_cache.clear()
    monkeypatch.setattr(files_router, "_boxes_cache_bytes", 0)
    return source_file


async def test_a_bundle_larger_than_the_budget_is_still_parsed_once(
    client, a_bundle_with_large_word_boxes, monkeypatch
) -> None:
    """Paging through a 300-page scan read and `json.loads`'d the whole
    artifact on every page turn, on the API's only event loop — and the
    artifact that broke the byte budget was served and dropped, so the one file
    the cache existed for was the one file it never held."""
    source_file = a_bundle_with_large_word_boxes
    parses = 0
    original = files_router._load_page_boxes

    def counting(path):
        nonlocal parses
        parses += 1
        return original(path)

    monkeypatch.setattr(files_router, "_load_page_boxes", counting)

    for page_number in (1, 2, 3, 4, 1):
        response = await client.get(
            f"/api/files/{source_file.id}/pages/{page_number}/boxes"
        )
        assert response.status_code == 200, response.text
        assert response.json()["number"] == page_number

    assert parses == 1, f"the artifact was re-parsed {parses} times over five page turns"


# ---------------------------------------------------------------------------
# CR-105 — /correspondents had no way for a client to ask for less
# ---------------------------------------------------------------------------


async def test_correspondents_can_be_narrowed_and_paged(client, session, signed_in):
    _, library = await signed_in()
    for name in ("Aetna", "Barclays", "Comcast", "Delta Dental"):
        session.add(
            Correspondent(library_id=library.id, name=name, slug=name.lower())
        )
    await session.commit()

    everything = (await client.get("/api/correspondents")).json()
    assert [row["name"] for row in everything] == [
        "Aetna", "Barclays", "Comcast", "Delta Dental"
    ], "the default is still the whole list — the edit panel's picker depends on it"

    page = (await client.get("/api/correspondents?limit=2&offset=1")).json()
    assert [row["name"] for row in page] == ["Barclays", "Comcast"]

    filtered = (await client.get("/api/correspondents?q=dent")).json()
    assert [row["name"] for row in filtered] == ["Delta Dental"]


async def test_correspondent_aliases_cost_one_query_not_one_each(
    client, session, signed_in
):
    """The N+1 this route used to be. Asserted by counting, because the fix is
    invisible in the response."""
    _, library = await signed_in()
    for name in ("Aetna", "Barclays", "Comcast"):
        session.add(Correspondent(library_id=library.id, name=name, slug=name.lower()))
    await session.commit()

    with capture_statements() as statements:
        response = await client.get("/api/correspondents")
    assert response.status_code == 200
    alias_reads = [s for s in statements if "correspondent_alias" in s.lower()]
    assert len(alias_reads) == 1, (
        f"{len(alias_reads)} alias queries for three correspondents; it should be one"
    )


# ---------------------------------------------------------------------------
# CR-106 — document_tag.tag_id led no index, so /tags scanned the link table
# ---------------------------------------------------------------------------


async def test_the_tag_count_reaches_an_index_that_leads_on_tag_id(session) -> None:
    plan = await _index_is_reachable(
        session,
        "SELECT count(*) FROM document_tag WHERE tag_id = "
        "'00000000-0000-0000-0000-000000000000' AND removed_at IS NULL",
        "ix_document_tag_tag_live",
    )
    assert "Seq Scan" not in plan


async def test_tag_counts_are_unchanged_by_the_grouped_join(
    client, session, signed_in
) -> None:
    """A correlated count per tag became one grouped join. The numbers, and the
    tags with none, must be exactly what they were."""
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id,
        sha256=uuid.uuid4().hex * 2,
        byte_size=512,
        original_filename="Statement.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    used = Tag(library_id=library.id, name="banking", slug="banking")
    unused = Tag(library_id=library.id, name="warranty", slug="warranty")
    revoked = Tag(library_id=library.id, name="old", slug="old")
    session.add_all([used, unused, revoked])
    await session.flush()
    document = Document(
        library_id=library.id,
        source_file_id=source_file.id,
        page_start=1,
        page_end=1,
        title="Statement",
    )
    session.add(document)
    await session.flush()
    session.add(
        DocumentTag(document_id=document.id, tag_id=used.id, source=TagSource.HUMAN)
    )
    session.add(
        DocumentTag(
            document_id=document.id,
            tag_id=revoked.id,
            source=TagSource.HUMAN,
            removed_at=sa.func.now(),
        )
    )
    await session.commit()

    rows = {row["name"]: row["document_count"] for row in (await client.get("/api/tags")).json()}
    assert rows["banking"] == 1
    # A tag nothing carries stays in the picker at (0). It is the near-duplicate
    # of a tag that does carry something, which is the whole reason counts are
    # shown here.
    assert rows["warranty"] == 0
    assert rows["old"] == 0, "a revoked link is history and must not be counted"


# ---------------------------------------------------------------------------
# CR-107 — the photo wall's ten leading-wildcard ILIKEs
# ---------------------------------------------------------------------------


async def test_the_photo_wall_filters_on_an_indexed_expression(session) -> None:
    await _index_is_reachable(
        session,
        r"SELECT count(*) FROM source_file "
        r"WHERE lower(substring(original_filename, '\.[^.]*$')) IN ('.jpg', '.png')",
        "ix_source_file_extension",
    )


@pytest.mark.parametrize(
    "filename,on_the_wall",
    [
        ("holiday.jpg", True),
        ("HOLIDAY.JPG", True),
        ("scan.tiff", True),
        ("statement.pdf", False),
        ("holiday.jpg.pdf", False),
        ("no-extension", False),
    ],
)
async def test_the_wall_holds_exactly_what_the_ilikes_held(
    client, session, signed_in, filename, on_the_wall
) -> None:
    """The suffix test changed shape, so its answers are pinned by name. Each
    of these is a case `original_filename ILIKE '%.jpg'` already decided; the
    expression must decide it the same way."""
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id,
        sha256=uuid.uuid4().hex * 2,
        byte_size=512,
        original_filename=filename,
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    session.add(
        Document(
            library_id=library.id,
            source_file_id=source_file.id,
            page_start=1,
            page_end=1,
            title=filename,
        )
    )
    session.add(Page(source_file_id=source_file.id, page_number=1, text=""))
    await session.commit()

    body = (await client.get("/api/photos")).json()
    names = [photo["original_filename"] for photo in body["photos"]]
    assert (filename in names) is on_the_wall


async def test_the_photo_wall_is_its_own_router() -> None:
    """CR-086: same path, same tag as the screen it serves."""
    from api.main import app

    operation = app.openapi()["paths"]["/api/photos"]["get"]
    assert operation["tags"] == ["photos"]


# ---------------------------------------------------------------------------
# CR-108 — six whole-archive counts on every keystroke and every page click
# ---------------------------------------------------------------------------


async def test_the_archive_header_is_one_statement(session, signed_in) -> None:
    from api.db import repository

    user, _ = await signed_in()
    bound = await repository.scope_for(session, user.id)
    with capture_statements() as statements:
        stats = await library_router._stats(session, bound)
    assert stats.documents == 0
    assert len(statements) == 1, (
        f"the header line took {len(statements)} statements; it was six, and "
        "none of them depends on the filter or the page number"
    )
    assert "FILTER" in statements[0].upper()


async def test_browsing_the_archive_scans_document_three_times_not_six(
    client, signed_in
) -> None:
    await signed_in()
    with capture_statements() as statements:
        response = await client.get("/api/archive")
    assert response.status_code == 200, response.text
    scans = [s for s in statements if TOUCHES_DOCUMENT.search(s)]
    # The count, the row page, and the header. The header used to be four more.
    assert len(scans) == 3, "\n---\n".join(scans)


# ---------------------------------------------------------------------------
# CR-111 / CR-112 — the two unindexed predicates
# ---------------------------------------------------------------------------


async def test_log_search_reaches_the_trigram_index(session) -> None:
    """`event_log` is deliberately never pruned, so the substring search has to
    be indexed rather than bounded."""
    await _index_is_reachable(
        session,
        "SELECT count(*) FROM event_log WHERE message ILIKE '%normalize failed%'",
        "ix_event_log_message_trgm",
    )


async def test_the_spend_window_reaches_an_index(session) -> None:
    await _index_is_reachable(
        session,
        "SELECT count(*) FROM classification "
        "WHERE created_at >= now() - interval '30 days'",
        "ix_classification_created_at",
    )
