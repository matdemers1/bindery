"""T-2.3 / T-2.5 / T-2.6 / T-2.8 — the segmentation API, export, and ranking."""

import hashlib
import uuid

import pikepdf
import pytest
import sqlalchemy as sa

from api.db.enums import IngestSource, MembershipRole, SourceFileState
from api.db.models import KnownForm, Page, SourceFile
from api.storage.blobs import blob_path

PAGES = {
    1: "CONSOLIDATED SERVICE RECORD cover sheet",
    2: "Continuation sheet with routine orders",
    3: "CERTIFICATE OF RELEASE OR DISCHARGE FROM ACTIVE DUTY DD FORM 214",
    4: "Separation date 2014-08-11 character of service honorable",
    5: "AMERICAN HONDA FINANCE payoff letter for the Accord",
}


def _minimal_pdf(page_count: int) -> bytes:
    """A real PDF with `page_count` pages, unique per call.

    The unique title matters: without it every fixture produces byte-identical
    output, and content-addressed storage would (correctly) treat the second
    test's bundle as a duplicate of the first's.
    """
    import io

    with pikepdf.Pdf.new() as pdf:
        for _ in range(page_count):
            pdf.add_blank_page(page_size=(612, 792))
        with pdf.open_metadata() as meta:
            meta["dc:title"] = f"fixture-{uuid.uuid4()}"
        buffer = io.BytesIO()
        pdf.save(buffer)
        return buffer.getvalue()


@pytest.fixture
async def bundle(session, signed_in):
    _, library = await signed_in()
    payload = _minimal_pdf(len(PAGES))
    sha = hashlib.sha256(payload).hexdigest()
    path = blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    source_file = SourceFile(
        library_id=library.id,
        sha256=sha,
        byte_size=len(payload),
        original_filename="Army Records 2019.pdf",
        ingest_source=IngestSource.WATCHED_FOLDER,
        page_count=len(PAGES),
        state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    for number, text in PAGES.items():
        session.add(Page(source_file_id=source_file.id, page_number=number, text=text))

    # The registry is global, so the first test to run creates it.
    existing = (
        await session.execute(sa.select(KnownForm).where(KnownForm.code == "DD-214"))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            KnownForm(
                code="DD-214",
                name="DD-214 — Certificate of Release or Discharge from Active Duty",
                match_rules={
                    "scope": "any_page",
                    "max_pages": 4,
                    "all": [
                        {"phrase": "certificate of release or discharge from active duty"}
                    ],
                },
            )
        )
    await session.commit()
    return library, source_file


async def _segment(client, source_file, spans):
    return await client.put(
        f"/api/files/{source_file.id}/segments",
        json={"segments": [
            {"page_start": a, "page_end": b, "title": t} for a, b, t in spans
        ]},
    )


# --------------------------------------------------------------------------
# Manual segmentation (REQ-036, REQ-037)
# --------------------------------------------------------------------------


async def test_a_new_file_can_be_cut_by_hand(client, bundle) -> None:
    _, source_file = bundle
    response = await _segment(
        client, source_file, [(1, 2, "Cover"), (3, 4, "DD-214"), (5, 5, "Honda payoff")]
    )
    assert response.status_code == 200
    body = response.json()
    assert [(s["page_start"], s["page_end"], s["title"]) for s in body["segments"]] == [
        (1, 2, "Cover"), (3, 4, "DD-214"), (5, 5, "Honda payoff")
    ]


async def test_a_cover_with_a_gap_is_refused_with_an_explanation(client, bundle) -> None:
    _, source_file = bundle
    response = await _segment(client, source_file, [(1, 2, None), (4, 5, None)])
    assert response.status_code == 422
    assert "belong to no segment" in response.json()["detail"]


async def test_an_overlapping_cover_is_refused(client, bundle) -> None:
    _, source_file = bundle
    response = await _segment(client, source_file, [(1, 3, None), (3, 5, None)])
    assert response.status_code == 422
    assert "overlap" in response.json()["detail"]


async def test_segmentation_matches_known_forms(client, bundle) -> None:
    """REQ-038 — a registry match is applied the moment boundaries are drawn."""
    _, source_file = bundle
    body = (await _segment(
        client, source_file, [(1, 2, "Cover"), (3, 4, "Discharge"), (5, 5, "Honda")]
    )).json()

    matched = [s for s in body["segments"] if s["known_form_id"]]
    assert len(matched) == 1
    assert (matched[0]["page_start"], matched[0]["page_end"]) == (3, 4)


async def test_moving_a_boundary_clears_a_stale_form_match(client, bundle) -> None:
    """A stale fact is worse than none: a fragment is not a DD-214."""
    _, source_file = bundle
    await _segment(client, source_file, [(1, 2, None), (3, 4, None), (5, 5, None)])

    # Absorb the discharge pages into a single 5-page catch-all, which exceeds
    # the form's max_pages.
    body = (await _segment(client, source_file, [(1, 5, "Everything")])).json()
    assert all(segment["known_form_id"] is None for segment in body["segments"])


async def test_undo_restores_the_previous_cut(client, bundle) -> None:
    _, source_file = bundle
    await _segment(client, source_file, [(1, 5, "Whole bundle")])
    await _segment(client, source_file, [(1, 2, "A"), (3, 5, "B")])

    body = (await client.post(f"/api/files/{source_file.id}/segments/undo")).json()
    assert [(s["page_start"], s["page_end"], s["title"]) for s in body["segments"]] == [
        (1, 5, "Whole bundle")
    ]


async def test_undo_with_nothing_to_undo_is_a_conflict(client, bundle) -> None:
    _, source_file = bundle
    response = await client.post(f"/api/files/{source_file.id}/segments/undo")
    assert response.status_code == 409


async def test_a_reader_cannot_segment(client, bundle, signed_in) -> None:
    _, source_file = bundle
    await client.post("/api/auth/logout")
    await signed_in(library_name="Read Only", role=MembershipRole.READER)
    assert (await _segment(client, source_file, [(1, 5, None)])).status_code in (403, 404)


async def test_another_library_s_file_cannot_be_segmented(client, bundle, signed_in) -> None:
    _, source_file = bundle
    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")
    assert (await _segment(client, source_file, [(1, 5, None)])).status_code == 404


# --------------------------------------------------------------------------
# Export (REQ-042)
# --------------------------------------------------------------------------


async def test_a_segment_exports_to_a_standalone_pdf(client, bundle) -> None:
    _, source_file = bundle
    before = blob_path(source_file.sha256).read_bytes()

    body = (await _segment(
        client, source_file, [(1, 2, "Cover"), (3, 4, "DD-214"), (5, 5, "Honda")]
    )).json()
    dd214 = next(s for s in body["segments"] if s["page_start"] == 3)

    response = await client.get(f"/api/documents/{dd214['id']}/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"

    import io

    with pikepdf.open(io.BytesIO(response.content)) as exported:
        # Exactly the segment's pages, no more.
        assert len(exported.pages) == 2

    # The export is a derivative artifact; the original is untouched.
    assert blob_path(source_file.sha256).read_bytes() == before


async def test_export_of_another_library_s_document_is_a_404(client, bundle, signed_in) -> None:
    _, source_file = bundle
    body = (await _segment(client, source_file, [(1, 5, "All")])).json()
    document_id = body["segments"][0]["id"]

    await client.post("/api/auth/logout")
    await signed_in(library_name="Someone Else")
    assert (await client.get(f"/api/documents/{document_id}/pdf")).status_code == 404


# --------------------------------------------------------------------------
# Search over segments (REQ-020, REQ-030, REQ-040, REQ-118)
# --------------------------------------------------------------------------


async def test_search_returns_the_document_not_the_bundle(client, bundle) -> None:
    """The Phase 2 payoff: a hit is "the DD-214", not "the file containing it"."""
    _, source_file = bundle
    await _segment(client, source_file, [(1, 2, "Cover"), (3, 4, "DD-214"), (5, 5, "Honda")])

    body = (await client.get("/api/search", params={"q": "separation"})).json()

    assert body["total"] == 1
    result = body["results"][0]
    assert result["title"] == "DD-214"
    assert (result["page_start"], result["page_end"]) == (3, 4)
    assert result["known_form_code"] == "DD-214"


async def test_page_numbers_are_disambiguated(client, bundle) -> None:
    """REQ-030 — "page 2 of this document (page 4 of the file)"."""
    _, source_file = bundle
    await _segment(client, source_file, [(1, 2, "Cover"), (3, 4, "DD-214"), (5, 5, "Honda")])

    result = (await client.get("/api/search", params={"q": "separation"})).json()["results"][0]

    assert result["best_page"]["page_number"] == 4           # in the file
    assert result["best_page"]["document_page_number"] == 2  # in the document
    assert result["file_page_count"] == 5


async def test_a_known_form_outranks_a_page_that_merely_mentions_it(client, bundle) -> None:
    """REQ-040."""
    _, source_file = bundle
    await _segment(client, source_file, [(1, 2, "Cover"), (3, 4, "Discharge"), (5, 5, "Honda")])

    body = (await client.get("/api/search", params={"q": "DD-214"})).json()

    assert body["total"] >= 1
    assert body["results"][0]["known_form_code"] == "DD-214"


async def test_known_form_appears_as_a_facet(client, bundle) -> None:
    _, source_file = bundle
    await _segment(client, source_file, [(1, 2, None), (3, 4, None), (5, 5, None)])

    body = (await client.get("/api/search", params={"q": "record OR discharge OR honda"})).json()
    forms = {facet["value"]: facet["count"] for facet in body["facets"]["known_form"]}
    assert forms.get("DD-214") == 1


async def test_search_can_be_scoped_to_one_bundle(client, bundle) -> None:
    """REQ-118."""
    _, source_file = bundle
    await _segment(client, source_file, [(1, 5, "All")])

    inside = await client.get(
        "/api/search", params={"q": "honda", "source_file_id": str(source_file.id)}
    )
    assert inside.json()["total"] == 1

    elsewhere = await client.get(
        "/api/search", params={"q": "honda", "source_file_id": str(uuid.uuid4())}
    )
    assert elsewhere.json()["total"] == 0


async def test_an_unsegmented_file_yields_no_search_results(client, bundle) -> None:
    """A page in no document is unreachable by document search — by design.

    The segment stage always produces a cover, so this only happens to a file
    still in the pipeline.
    """
    body = (await client.get("/api/search", params={"q": "separation"})).json()
    assert body["total"] == 0
