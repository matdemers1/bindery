"""REQ-025 — search p95 under 300 ms at 100K pages.

Opt in with `-m slow`; seeding takes a couple of minutes and this does not need
to run on every change. The budget comes from the ten-second success criterion:
search 300 ms, viewer 2 s, palette 100 ms — the rest is the human.

    docker compose --env-file .env -f infra/docker-compose.yml --profile test \
      run --rm test python -m pytest tests/test_search_performance.py -m slow -s
"""

import statistics
import time

import pytest
import sqlalchemy as sa

from api.search import query as search_query

FILES = 1000
PAGES_PER_FILE = 100
TOTAL_PAGES = FILES * PAGES_PER_FILE

# Queries spanning what a real archive looks like: a form code that appears on a
# handful of pages, a correspondent on a few hundred, an ordinary word on a few
# thousand. Deliberately *not* a term that appears on every page — see
# test_a_term_on_every_page_is_reported_not_asserted.
QUERIES = [
    "dd214",
    '"certificate of release"',
    "honda accord",
    "geico declarations",
    "mortgage principal",
    "tinnitus",
    "odometer",
    "quitclaim",
    "separation discharge",
    "escrow",
]

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
async def seeded(session_module, library_module):
    """100K pages of synthetic but lexically varied text."""
    await session_module.execute(
        sa.text("""
        INSERT INTO source_file
            (id, library_id, sha256, byte_size, original_filename, ingest_source,
             ingest_metadata, page_count, state, received_at)
        SELECT gen_random_uuid(), :library, md5(f::text) || md5((f + 1)::text),
               100000, 'seed-' || f || '.pdf', 'bulk_import', '{}'::jsonb,
               :pages, 'processed', now()
        FROM generate_series(1, :files) f
        """),
        {"library": library_module.id, "files": FILES, "pages": PAGES_PER_FILE},
    )
    # Text designed to behave like a real archive: a large vocabulary of
    # per-page filler so terms are selective, with the interesting phrases
    # sprinkled across a small fraction of pages. Seeding every page with one of
    # five phrases instead would make ordinary queries match 20% of the corpus,
    # the GIN index useless, and the measurement meaningless.
    await session_module.execute(
        sa.text("""
        INSERT INTO page (id, source_file_id, page_number, text)
        SELECT gen_random_uuid(), sf.id, p,
               'Page ' || p || ' of ' || sf.original_filename || '. ' ||
               'ref' || (n % 40009) || ' lot' || (n % 8017) || ' item' || (n % 1601) ||
               ' entry' || (n % 313) || ' section' || (n % 97) || ' ' ||
               CASE
                 WHEN n % 997 = 0 THEN
                   'certificate of release or discharge from active duty dd214 '
                   'separation date character of service honorable'
                 WHEN n % 389 = 0 THEN
                   'geico automobile insurance declarations honda accord policy period'
                 WHEN n % 421 = 0 THEN
                   'mortgage interest statement outstanding mortgage principal escrow'
                 WHEN n % 613 = 0 THEN
                   'department of veterans affairs rating decision tinnitus evaluation'
                 WHEN n % 733 = 0 THEN
                   'certificate of title vehicle identification number odometer reading'
                 WHEN n % 881 = 0 THEN
                   'quitclaim deed grantor conveys and warrants register of deeds'
                 ELSE 'continuation sheet administrative correspondence filed in sequence'
               END
        FROM source_file sf,
             LATERAL (SELECT generate_series(1, :pages) AS p) g,
             LATERAL (SELECT ('x' || substr(md5(sf.id::text || g.p::text), 1, 8))::bit(32)::bigint
                      AS n) h
        WHERE sf.library_id = :library
        """),
        {"library": library_module.id, "pages": PAGES_PER_FILE},
    )
    # Search rolls page hits up to documents (ADR-001), so pages with no
    # document produce no results — and an empty result set would make this
    # measure nothing at all. Each file is cut into four segments, so the
    # page-to-document join has real work to do.
    await session_module.execute(
        sa.text("""
        INSERT INTO document
            (id, library_id, source_file_id, page_start, page_end, title,
             sensitivity, redundancy, review_state, is_backlog, created_at, updated_at)
        SELECT gen_random_uuid(), :library, sf.id,
               s.start_page, s.start_page + (:pages / 4) - 1,
               'Segment ' || s.start_page || ' of ' || sf.original_filename,
               'normal', 'local', 'pending_classification', false, now(), now()
        FROM source_file sf
        CROSS JOIN (
            SELECT generate_series(1, :pages, :pages / 4) AS start_page
        ) s
        WHERE sf.library_id = :library
        """),
        {"library": library_module.id, "pages": PAGES_PER_FILE},
    )
    await session_module.commit()
    await session_module.execute(sa.text("ANALYZE page"))
    await session_module.execute(sa.text("ANALYZE source_file"))
    await session_module.execute(sa.text("ANALYZE document"))
    await session_module.commit()

    count = (await session_module.execute(sa.text("SELECT count(*) FROM page"))).scalar_one()
    documents = (
        await session_module.execute(sa.text("SELECT count(*) FROM document"))
    ).scalar_one()
    assert count >= TOTAL_PAGES
    # Guard against the failure mode this seed once had: with no documents the
    # join eliminates everything and the test measures an empty result set.
    assert documents >= FILES * 4
    return count


async def test_search_p95_is_within_budget(seeded, session_module, library_module) -> None:
    visible = [library_module.id]
    # Warm the cache the way a real session would be warm.
    for query in QUERIES:
        await search_query.search(session_module, query, visible, limit=25)

    timings: list[float] = []
    for _ in range(5):
        for query in QUERIES:
            started = time.perf_counter()
            await search_query.search(session_module, query, visible, limit=25)
            timings.append((time.perf_counter() - started) * 1000)

    timings.sort()
    p50 = statistics.median(timings)
    p95 = timings[int(len(timings) * 0.95) - 1]
    print(
        f"\n{seeded:,} pages | p50 {p50:.0f} ms | p95 {p95:.0f} ms | max {timings[-1]:.0f} ms"
    )

    assert p95 < 300, f"search p95 {p95:.0f} ms exceeds the 300 ms budget (REQ-025)"


async def test_a_term_on_every_page_is_reported_not_asserted(
    seeded, session_module, library_module
) -> None:
    """The pathological case, measured but deliberately not a gate.

    A term matching a fifth of the archive makes the GIN index worthless and
    returns thousands of documents. Nobody searches that way expecting speed,
    and holding it to the 300 ms budget would mean tuning for a query that has
    no useful answer. It is measured so a regression is still visible.
    """
    started = time.perf_counter()
    response = await search_query.search(
        session_module, "continuation sheet", [library_module.id], limit=25
    )
    elapsed = (time.perf_counter() - started) * 1000
    print(f"\nworst case: 'continuation sheet' matched {response.total:,} documents "
          f"in {elapsed:.0f} ms")


async def test_palette_latency_is_within_budget(
    seeded, session_module, library_module
) -> None:
    """REQ-027 — the command palette must answer in under 100 ms.

    A third of the search budget, because the palette runs on every keystroke.
    It gets there by asking for far fewer rows: the same query, `limit=8`,
    which is what fits on the screen anyway.
    """
    visible = [library_module.id]
    for query in QUERIES:
        await search_query.search(session_module, query, visible, limit=8)

    timings: list[float] = []
    for _ in range(5):
        for query in QUERIES:
            started = time.perf_counter()
            await search_query.search(session_module, query, visible, limit=8)
            timings.append((time.perf_counter() - started) * 1000)

    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    print(f"\npalette | p50 {statistics.median(timings):.0f} ms | p95 {p95:.0f} ms")
    assert p95 < 100, f"palette p95 {p95:.0f} ms exceeds the 100 ms budget (REQ-027)"


async def test_ask_retrieval_stays_inside_the_search_budget(
    seeded, session_module, library_module
) -> None:
    """Q&A retrieval broadens to an OR query when the precise one is empty, and
    an OR across content words is the expensive shape. Measured here so the
    fallback cannot quietly become the slow path nobody notices.

    Split the same way the search suite splits: an ordinary question is a gate,
    a question built out of a term matching a fifth of the archive is reported.
    Holding the second to a budget would mean tuning for a query with no useful
    answer, and the existing `continuation sheet` case already documents that
    cost at the search layer.
    """
    from api import ask

    ordinary = [
        "when did I last get the brakes done?",
        "what is my policy number?",
        "how much did the roof cost?",
    ]
    timings: list[float] = []
    for question in ordinary:
        started = time.perf_counter()
        await ask.gather_sources(session_module, question, [library_module.id])
        timings.append((time.perf_counter() - started) * 1000)

    worst = max(timings)
    print(f"\nask retrieval | worst {worst:.0f} ms across {len(timings)} questions")

    started = time.perf_counter()
    await ask.gather_sources(
        session_module, "how much was the continuation sheet?", [library_module.id]
    )
    pathological = (time.perf_counter() - started) * 1000
    print(f"ask retrieval worst case: 'continuation sheet' in {pathological:.0f} ms")

    # Two search passes at worst — precise, then broad — so twice the budget.
    assert worst < 600, f"ask retrieval {worst:.0f} ms is beyond two search budgets"


def test_the_broad_fallback_is_bounded() -> None:
    """The OR pass uses only the most selective terms.

    Not a latency gate — the measurement above shows breadth is not what costs,
    a single common term is — but an unbounded OR over a long question would
    return the archive in relevance order, which is not an answer.
    """
    from api import ask

    precise, broad = ask.question_to_query(
        "how much did the emergency roof replacement contractor invoice cost me"
    )
    assert broad.count(" OR ") + 1 <= ask.BROAD_TERMS
    assert len(precise.split()) > ask.BROAD_TERMS, "the precise pass keeps every word"


def test_a_question_of_only_stopwords_does_not_match_everything() -> None:
    from api import ask

    precise, broad = ask.question_to_query("what is it?")
    assert precise == broad == "what is it?"
