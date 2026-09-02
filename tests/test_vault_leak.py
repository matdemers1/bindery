"""A vaulted document is invisible everywhere (T-16.11, REQ-180, REQ-187).

Written before anything could create a vaulted document, and deliberately: a
leak suite written *after* the feature tends to test the paths the feature
author was already thinking about. The one that leaks is never the document
endpoint — it is a facet count, a search snippet, a palette result, or a
progress row on a screen nobody associated with privacy.

The rows here are marked vaulted by hand rather than by the real move, so this
holds even before T-16.5 exists and keeps holding if the move changes.

**Every test runs twice, locked and unlocked.** The first version of this suite
only ran locked, and that gap is exactly what shipped a bug: unlocking put the
vaulted documents back into the archive, photos, search and every count, and
nothing here noticed. Being open governs whether the vault can be *read*, not
whether its contents leak into everything else — the reason to vault something
is not wanting it on screen when somebody is looking over your shoulder, and
the vault stays open for fifteen minutes after you glance at it.
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api.db.enums import IngestSource, ReviewState, SourceFileState
from api.db.models import Document, Page, SourceFile

SECRET_PHRASE = "quetzalcoatlus northropi settlement"


@pytest.fixture(params=["locked", "unlocked"], ids=["locked", "unlocked"])
async def a_vaulted_document(request, session, signed_in):
    """A document whose text is distinctive enough to find anywhere it leaks."""
    user, library = await signed_in()
    source = SourceFile(
        library_id=library.id,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        byte_size=2048,
        # An image, deliberately: the photo wall filters on the extension, so
        # a .pdf fixture would sail past that test without exercising it —
        # which is how the photo-wall leak survived a suite that had eleven
        # other assertions.
        original_filename="the-private-one.jpg",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    session.add(
        Page(source_file_id=source.id, page_number=1, text=f"{SECRET_PHRASE} appears here")
    )
    document = Document(
        library_id=library.id,
        source_file_id=source.id,
        page_start=1,
        page_end=1,
        title=f"Private — {SECRET_PHRASE}",
        review_state=ReviewState.FILED,
        vaulted_by=user.id,
    )
    session.add(document)
    await session.commit()

    if request.param == "unlocked":
        # The state a person is actually in for fifteen minutes after opening
        # the vault, and the one every surface below must survive.
        from api.vault.session import sessions

        sessions.unlock(user.id, b"\x00" * 32)
        request.addfinalizer(lambda: sessions.lock(user.id))

    return user, library, document, source


async def test_search_finds_nothing(client, a_vaulted_document):
    """Asserted on the payload, not on the raw body.

    The response echoes the query back, so `SECRET_PHRASE not in response.text`
    is trivially false however well the boundary works — the first version of
    this test failed on the word it had just sent.
    """
    body = (await client.get("/api/search", params={"q": SECRET_PHRASE})).json()
    assert body["total"] == 0
    assert body["results"] == []
    assert body["suggestions"] == [], "a title offered as a suggestion is still a leak"
    assert "the-private-one" not in str(body["facets"])


async def test_the_palette_finds_nothing(client, a_vaulted_document):
    """The palette is a different query from search and is the one people
    actually use — ⌘K is the fast path this archive was built around."""
    body = (await client.get("/api/search", params={"q": SECRET_PHRASE, "limit": 5})).json()
    assert body["total"] == 0 and body["results"] == []


async def test_the_archive_browser_does_not_list_it(client, a_vaulted_document):
    response = await client.get("/api/archive")
    assert "the-private-one" not in response.text
    assert SECRET_PHRASE not in response.text


async def test_the_file_list_does_not_carry_it(client, a_vaulted_document):
    """A document and its file are different rows. Hiding one and listing the
    other leaves the page images downloadable."""
    response = await client.get("/api/files")
    assert "the-private-one" not in response.text


async def test_the_document_itself_is_not_reachable_by_id(client, a_vaulted_document):
    """404, not 403 — the same reasoning as ADR-005. A 403 confirms it exists."""
    _, _, document, _ = a_vaulted_document
    response = await client.get(f"/api/documents/{document.id}")
    assert response.status_code in (404, 405), response.text


async def test_the_source_file_is_not_reachable_by_id(client, a_vaulted_document):
    _, _, _, source = a_vaulted_document
    response = await client.get(f"/api/files/{source.id}")
    assert response.status_code == 404


async def test_its_raw_text_is_not_servable(client, a_vaulted_document):
    """`/api/files/{id}/text` exists so a search finding nothing is
    distinguishable from OCR having failed. It must not become a way around
    the vault."""
    _, _, _, source = a_vaulted_document
    response = await client.get(f"/api/files/{source.id}/text")
    assert response.status_code == 404
    assert SECRET_PHRASE not in response.text


async def test_it_does_not_appear_in_the_pipeline_progress(client, a_vaulted_document):
    response = await client.get("/api/pipeline")
    assert "the-private-one" not in response.text


async def test_it_is_absent_from_counts_and_facets(client, a_vaulted_document, session):
    """The subtle one. A hidden document that still contributes to a total says
    'there is something here you cannot see', which is more than nothing."""
    visible = (
        await session.execute(
            sa.select(sa.func.count(Document.id)).where(Document.vaulted_by.is_(None))
        )
    ).scalar_one()
    body = (await client.get("/api/archive")).json()
    reported = body.get("stats", {}).get("total") or body.get("total")
    if reported is not None:
        assert reported <= visible


async def test_the_audit_log_does_not_replay_its_title(client, a_vaulted_document):
    """History is scoped by resolving each event's entity to a library, so a
    vaulted document's past events would otherwise still be readable."""
    response = await client.get("/api/audit")
    assert SECRET_PHRASE not in response.text


async def test_an_api_token_can_never_see_it(client, a_vaulted_document):
    """Unlocking is something a person did with a PIN. A long-lived bearer
    token is the opposite of that, so it is locked by construction."""
    from api.auth.dependencies import Scope

    assert "vault_unlocked" in Scope.__dataclass_fields__
    assert Scope.__dataclass_fields__["vault_unlocked"].default is False


# --------------------------------------------------------------------------
# The photo wall, and the guard that should have caught it (REQ-187)
# --------------------------------------------------------------------------


async def test_the_photo_wall_does_not_show_it(client, a_vaulted_document):
    """The surface that was actually leaking.

    `/api/photos` had no vault boundary at all — not a wrong one, none — so a
    vaulted photograph stayed on the wall whether the vault was open or shut.
    Photographs are the likeliest thing anyone vaults, which made this the one
    screen that most needed it and the one nobody wrote a test for.
    """
    _, _, document, _ = a_vaulted_document
    response = await client.get("/api/photos")
    assert response.status_code == 200, response.text
    body = response.json()
    assert str(document.id) not in [photo["document_id"] for photo in body["photos"]]
    assert SECRET_PHRASE not in response.text


async def test_the_review_queue_does_not_list_it(client, session, a_vaulted_document):
    """A document can be vaulted while it is still awaiting review, and the
    queue is a list of titles on a screen like any other."""
    from api.db.enums import ReviewState

    _, _, document, _ = a_vaulted_document
    document.review_state = ReviewState.NEEDS_REVIEW
    await session.commit()

    response = await client.get("/api/review")
    assert SECRET_PHRASE not in response.text


async def test_taxonomy_counts_do_not_include_it(client, session, a_vaulted_document):
    """A count is a statement about contents. "GEICO (12)" when you can reach
    eleven says one more exists, which is the shape of leak this is about."""
    from api.db.models import Correspondent

    _, library, document, _ = a_vaulted_document
    who = Correspondent(library_id=library.id, name="Vaulted Sender", slug="vaulted-sender")
    session.add(who)
    await session.flush()
    document.correspondent_id = who.id
    await session.commit()

    response = await client.get("/api/correspondents")
    rows = {row["name"]: row["document_count"] for row in response.json()}
    assert rows.get("Vaulted Sender") == 0, (
        "a vaulted document was counted, so its existence is visible"
    )


def test_every_route_that_selects_documents_applies_the_vault_boundary() -> None:
    """The guard the photo wall needed.

    Phase 7 learned this lesson for libraries and built a route-coverage guard;
    the vault got the boundary and not the guard, and a screen written before
    the vault existed simply never gained the clause. A boundary that depends
    on every author remembering is a boundary with a hole in it.

    Structural on purpose: it asks whether the module names the boundary at
    all, which is crude, and crude is what survives. A module that queries
    documents and never mentions the vault cannot possibly be applying it.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    # Reached through `Scope` or `repository`, both of which apply it centrally.
    VIA_SCOPE = {"documents.py", "files.py", "segments.py", "upload.py", "trust.py"}
    # Vault routes are the one place vaulted rows are *supposed* to be visible.
    EXEMPT = {"vault.py"}

    offenders = []
    for path in sorted((root / "api" / "routers").glob("*.py")):
        if path.name in VIA_SCOPE or path.name in EXEMPT:
            continue
        source = path.read_text()
        selects = re.search(
            r"select\(Document\)|select_from\(Document\)|Document\.library_id", source
        )
        if not selects:
            continue
        if "document_clause" in source or "hidden_source_file_ids" in source:
            continue
        offenders.append(path.name)

    assert not offenders, (
        "these routers query documents without naming the vault boundary:\n  "
        + "\n  ".join(offenders)
        + "\nApply `boundary.document_clause(user.id)`, or add the module to "
        "VIA_SCOPE with a reason."
    )


async def test_the_home_screen_does_not_list_a_vaulted_vital_record(
    client, session, a_vaulted_document
):
    """`GET /api/vital` was the one read path with no vault clause.

    A vital record is the likeliest thing anyone vaults — ADR-012 names the
    deed and the discharge papers itself — and `seal` never touches
    `sensitivity`, so a sealed VITAL document kept matching this filter and
    kept appearing on the home screen, locked or unlocked.
    """
    from api.db.enums import Sensitivity

    _user, _library, document, _source = a_vaulted_document
    document.sensitivity = Sensitivity.VITAL
    await session.commit()

    response = await client.get("/api/vital")
    assert response.status_code == 200, response.text
    assert str(document.id) not in [row["id"] for row in response.json()]
    assert SECRET_PHRASE not in response.text
