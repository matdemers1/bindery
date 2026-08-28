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

# Queries chosen to span the range: rare terms, common terms, phrases, and a
# term that appears in nearly every page (the pathological case for ranking).
QUERIES = [
    "dd214",
    "separation",
    "honda accord",
    '"active duty"',
    "insurance",
    "statement account",
    "invoice",
    "brake rotors",
    "certificate release discharge",
    "utility",
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
    await session_module.execute(
        sa.text("""
        INSERT INTO page (id, source_file_id, page_number, text)
        SELECT gen_random_uuid(), sf.id, p,
               'Page ' || p || ' of ' || sf.original_filename || '. ' ||
               (ARRAY['certificate of release or discharge from active duty dd214 separation',
                      'geico automobile insurance declarations honda accord policy',
                      'water utility statement account balance due quarter',
                      'honda service invoice front brake pads and rotors replaced',
                      'consolidated service record cover sheet department of defense'
                     ])[1 + (p % 5)] || ' ' ||
               'filler token ' || (p * 7919 % 4001)::text
        FROM source_file sf, generate_series(1, :pages) p
        WHERE sf.library_id = :library
        """),
        {"library": library_module.id, "pages": PAGES_PER_FILE},
    )
    await session_module.commit()
    await session_module.execute(sa.text("ANALYZE page"))
    await session_module.execute(sa.text("ANALYZE source_file"))
    await session_module.commit()

    count = (await session_module.execute(sa.text("SELECT count(*) FROM page"))).scalar_one()
    assert count >= TOTAL_PAGES
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
