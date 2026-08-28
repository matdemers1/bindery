"""T-1.6 — page-anchored search, and the scoping around it.

The property that must never regress: a document must never become unfindable,
and must never leak across a library boundary.
"""

import uuid

import pytest

from api.db.enums import IngestSource, LibraryKind, SourceFileState
from api.db.models import Document, Library, Page, SourceFile, Tag

BUNDLE_PAGES = {
    1: "Cover sheet for the consolidated service record.",
    12: "GEICO automobile insurance declarations page for the Honda Accord.",
    47: "CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY DD214 separation date 2014-08-11",
    63: "Water utility statement for the quarter. Account balance due.",
    88: "Honda service invoice: front brake pads and rotors replaced.",
}


@pytest.fixture
async def bundle(session, signed_in):
    """A 100-page bundle whose DD-214 is on page 47 — the whole product bet."""
    user, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id,
        sha256=uuid.uuid4().hex * 2,
        byte_size=1024,
        original_filename="Army Records 2019.pdf",
        ingest_source=IngestSource.WATCHED_FOLDER,
        page_count=100,
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    for number, text in BUNDLE_PAGES.items():
        session.add(
            Page(
                source_file_id=source_file.id,
                page_number=number,
                text=text,
                render_path=f"derived/x/pages/{number:04d}.webp",
                thumb_path=f"derived/x/thumbs/{number:04d}.webp",
            )
        )
    # From Phase 2 on, search rolls page hits up to documents (ADR-001), so an
    # un-segmented file has nothing to return. This bundle is deliberately left
    # whole — one document over all 100 pages — because these tests are about
    # the text layer, not the cut.
    session.add(
        Document(
            library_id=library.id,
            source_file_id=source_file.id,
            page_start=1,
            page_end=100,
            title="Army Records 2019",
        )
    )
    await session.commit()
    return user, library, source_file


async def test_search_returns_the_page_the_phrase_is_on(client, bundle) -> None:
    """REQ-020. The answer is 'page 47 of <file>', not 'somewhere in this file'."""
    _, _, source_file = bundle

    body = (await client.get("/api/search", params={"q": "discharge from active duty"})).json()

    assert body["total"] == 1
    result = body["results"][0]
    assert result["source_file_id"] == str(source_file.id)
    assert result["original_filename"] == "Army Records 2019.pdf"
    assert result["best_page"]["page_number"] == 47


async def test_snippet_marks_the_matched_terms(client, bundle) -> None:
    body = (await client.get("/api/search", params={"q": "separation"})).json()
    snippet = body["results"][0]["best_page"]["snippet"]
    assert "<mark>" in snippet and "</mark>" in snippet


async def test_a_bundle_is_one_result_with_its_best_page(client, bundle) -> None:
    """Thirty hits in one file must not flood thirty rows into the results."""
    body = (await client.get("/api/search", params={"q": "honda"})).json()

    assert body["total"] == 1
    result = body["results"][0]
    assert result["matching_pages"] == 2
    assert result["best_page"]["page_number"] in (12, 88)


async def test_quoted_phrases_and_exclusions_work(client, bundle) -> None:
    both = (await client.get("/api/search", params={"q": "honda"})).json()
    assert both["total"] == 1

    excluded = (await client.get("/api/search", params={"q": "honda -brake -insurance"})).json()
    assert excluded["total"] == 0


async def test_a_nonsense_query_never_raises(client, bundle) -> None:
    """The query box is wired to every keystroke; it cannot 500 on a stray token."""
    for query in ["&&&", '"unclosed', "a & | b", "!!", "()"]:
        response = await client.get("/api/search", params={"q": query})
        assert response.status_code == 200, query


async def test_empty_query_returns_nothing_rather_than_everything(client, bundle) -> None:
    body = (await client.get("/api/search", params={"q": "   "})).json()
    assert body["total"] == 0


async def test_search_requires_authentication(client) -> None:
    await client.post("/api/auth/logout")
    assert (await client.get("/api/search", params={"q": "dd214"})).status_code == 401


async def test_search_never_crosses_a_library_boundary(client, bundle, signed_in) -> None:
    """The one that must never regress."""
    assert (await client.get("/api/search", params={"q": "dd214"})).json()["total"] == 1

    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")

    body = (await client.get("/api/search", params={"q": "dd214"})).json()
    assert body["total"] == 0
    assert body["results"] == []


async def test_requesting_an_invisible_library_cannot_widen_scope(
    client, bundle, session
) -> None:
    """A hand-written library_id may only ever narrow, never widen."""
    stranger = Library(name="Not Mine", kind=LibraryKind.PERSONAL)
    session.add(stranger)
    await session.commit()

    body = (
        await client.get(
            "/api/search", params={"q": "dd214", "library_id": str(stranger.id)}
        )
    ).json()
    assert body["total"] == 0


async def test_filtering_to_one_file_scopes_the_search(client, bundle) -> None:
    _, _, source_file = bundle
    inside = await client.get(
        "/api/search", params={"q": "dd214", "source_file_id": str(source_file.id)}
    )
    assert inside.json()["total"] == 1

    elsewhere = await client.get(
        "/api/search", params={"q": "dd214", "source_file_id": str(uuid.uuid4())}
    )
    assert elsewhere.json()["total"] == 0


async def test_facets_count_the_matched_set(client, bundle) -> None:
    _, library, _ = bundle
    body = (await client.get("/api/search", params={"q": "honda"})).json()

    libraries = {facet["value"]: facet["count"] for facet in body["facets"]["library"]}
    assert libraries == {str(library.id): 1}
    states = {facet["value"]: facet["count"] for facet in body["facets"]["state"]}
    assert states == {"processed": 1}


async def test_trigram_suggests_a_correction_when_nothing_matched(
    client, bundle, session
) -> None:
    """REQ-023 — 'Hoda' finds 'Honda', which FTS alone will never do."""
    _, library, _ = bundle
    session.add(Tag(library_id=library.id, name="Honda", slug="honda"))
    await session.commit()

    body = (await client.get("/api/search", params={"q": "Hoda"})).json()

    assert body["total"] == 0
    assert any("Honda" in suggestion for suggestion in body["suggestions"])


async def test_suggestions_are_silent_when_there_are_results(client, bundle) -> None:
    body = (await client.get("/api/search", params={"q": "honda"})).json()
    assert body["suggestions"] == []
