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
import json
import uuid

import pytest

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
    'there is something here you cannot see', which is more than nothing.

    Pinned to an exact number, and compared like with like. The first version of
    this asserted `reported <= visible`, where `visible` counted every
    non-vaulted document in the whole shared test database — hundreds by the
    time this file runs — and `reported` was one library's total. Removing the
    vault boundary entirely moved `reported` from 1 to 2 and left it three
    orders of magnitude under `visible`, so the one assertion guarding the
    hardest-to-spot leak in the suite could not fail. It also hid behind
    `if reported is not None`, which meant reshaping `ArchiveStatsOut` would
    have skipped it silently rather than failing.
    """
    _user, library, _document, _source = a_vaulted_document

    # One ordinary filed document in the *same* library, so the expected total
    # is a number rather than an inequality.
    ordinary_file = SourceFile(
        library_id=library.id,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        byte_size=1024,
        original_filename="the-ordinary-one.jpg",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(ordinary_file)
    await session.flush()
    session.add(Page(source_file_id=ordinary_file.id, page_number=1, text="nothing secret"))
    session.add(
        Document(
            library_id=library.id,
            source_file_id=ordinary_file.id,
            page_start=1,
            page_end=1,
            title="Ordinary — the one that should be counted",
            review_state=ReviewState.FILED,
        )
    )
    await session.commit()

    body = (await client.get("/api/archive")).json()

    assert body["total"] == 1, (
        "the archive total counts the vaulted document. A hidden row that still "
        f"moves a total says there is something here you cannot see: {body['total']}"
    )
    stats = body["stats"]
    assert stats["documents"] == 1, f"stats.documents counted the vault: {stats}"
    assert stats["files"] == 1, f"stats.files counted the vaulted file: {stats}"
    assert stats["pages"] == 1, f"stats.pages counted the vaulted page: {stats}"
    # And the entries agree with the counts, so a total that is right by
    # coincidence while the list is wrong still fails.
    assert [entry["title"] for entry in body["entries"]] == [
        "Ordinary — the one that should be counted"
    ]


# --------------------------------------------------------------------------
# Q&A — the one surface that returns page text verbatim (CR-040)
# --------------------------------------------------------------------------
#
# Every other leak here exposes a title, a count or a thumbnail. Ask quotes the
# *contents* of the page back, with a citation, and — with a key configured —
# transmits it to Anthropic on the way. It is the surface a vaulted medical or
# discharge record must reach least, and it was in neither leak suite: the
# protection is one keyword argument, `viewer=`, and dropping it while
# refactoring would have broken nothing that any test or guard observed.
#
# The question deliberately does not contain SECRET_PHRASE. The response echoes
# the question back, so a body-substring assertion on a phrase we just sent is
# trivially false however well the boundary works — the same trap
# `test_search_finds_nothing` documents.

ASK_QUESTION = "What settlement did the northropi paperwork describe?"


async def test_ask_does_not_quote_a_vaulted_page(client, a_vaulted_document):
    """The endpoint, both locked and unlocked."""
    _user, _library, document, source = a_vaulted_document

    response = await client.post("/api/ask", json={"question": ASK_QUESTION})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["consulted"] == [], (
        "Ask retrieved the vaulted document as a source. Its page text is what "
        "gets sent to the model and quoted back on screen."
    )
    assert body["citations"] == []
    assert body["answer"] is None, "there was nothing citable to answer from"

    # Everything except the echoed question, so the assertion is about what the
    # archive said rather than about what we asked.
    payload = json.dumps({key: value for key, value in body.items() if key != "question"})
    assert SECRET_PHRASE not in payload
    assert "the-private-one" not in payload
    assert str(document.id) not in payload
    assert str(source.id) not in payload


async def test_ask_retrieval_hides_the_vault_from_the_service_layer(
    session, a_vaulted_document
):
    """One layer down, where the boundary actually is.

    `gather_sources` runs two queries: a vault-filtered search, and then a
    second `select(Document)` narrowed to the ids that search returned. The
    second is safe only transitively, so it is asserted here directly — a
    refactor that widened it would still pass the route test above for as long
    as the first query kept the ids out.
    """
    from api import ask as ask_module

    user, library, _document, _source = a_vaulted_document

    sources = await ask_module.gather_sources(
        session, ASK_QUESTION, [library.id], viewer=user.id
    )
    assert sources == [], (
        "gather_sources returned the vaulted page to the layer that builds the "
        "prompt: " + str([source.title for source in sources])
    )


async def test_ask_fails_closed_when_the_caller_forgets_the_viewer(
    session, a_vaulted_document
):
    """`viewer` defaults to `None`, and the default must not mean 'no filter'.

    This is the shape of the defect that put five read paths on the wrong side
    of the boundary: an opt-in whose default was fail-open. A new caller of
    `gather_sources` that says nothing about who is asking must get nothing
    from the vault, not everything.
    """
    from api import ask as ask_module

    _user, library, _document, _source = a_vaulted_document

    sources = await ask_module.gather_sources(session, ASK_QUESTION, [library.id])
    assert sources == [], (
        "a caller that did not pass `viewer=` was handed vaulted page text"
    )


async def test_the_audit_log_does_not_replay_its_title(client, a_vaulted_document):
    """History is scoped by resolving each event's entity to a library, so a
    vaulted document's past events would otherwise still be readable."""
    response = await client.get("/api/audit")
    assert SECRET_PHRASE not in response.text


async def test_an_api_token_can_never_see_it(session, a_vaulted_document):
    """Unlocking is something a person did with a PIN. A long-lived bearer
    token is the opposite of that, so it is locked by construction.

    Asserted on what the boundary returns, not on the existence of a
    `Scope.vault_unlocked` field (CR-084). That field was resolved on every
    request and read by nothing, so the assertion protected a value that
    influenced no answer — and would have gone on passing while an unlock put
    the document back into every view.
    """
    import sqlalchemy as sa

    from api.db.scope import for_libraries

    _, library, document, _ = a_vaulted_document
    # The boundary an API token gets: library ids and no session behind them.
    scope = for_libraries([library.id])
    visible = (
        await session.execute(sa.select(Document.id).where(scope.only(Document)))
    ).scalars().all()
    assert document.id not in visible


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


# Routers that reach documents through `Scope` or `repository`, both of which
# apply the boundary centrally — each with the reason, so an addition is a
# written claim rather than a name appearing in a set. `library.py` and
# `logs.py` were originally passing this guard on a substring match against the
# word "vault" in a *comment*, which is why the reasons are here at all.
VIA_SCOPE = {
    "documents.py": "every query goes through `repository`, which carries both halves",
    "files.py": "`repository.get_source_file` / `list_pages`; no query of its own",
    "segments.py": "reaches one file the caller already resolved through `Scope`",
    "upload.py": "writes; it selects nothing it did not just create",
    "trust.py": "asks `Scope` for the rows the export and the vital list read",
    "library.py": "the archive browser, built entirely on `scope.only(Document)`",
    "photos.py": "the photo wall; every query is `bound.only(Document)` (REQ-187)",
}
# Vault routes are the one place vaulted rows are *supposed* to be visible.
EXEMPT_FROM_VAULT_BOUNDARY = {
    "vault.py": "the vault's own routes — reading vaulted rows is what they are for",
}


def test_every_route_that_selects_documents_applies_the_vault_boundary() -> None:
    """The guard the photo wall needed.

    Phase 7 learned this lesson for libraries and built a route-coverage guard;
    the vault got the boundary and not the guard, and a screen written before
    the vault existed simply never gained the clause. A boundary that depends
    on every author remembering is a boundary with a hole in it.

    Structural on purpose: it asks whether the module names the boundary at
    all, which is crude, and crude is what survives. A module that queries
    documents and never mentions the vault cannot possibly be applying it.

    **Scope, stated plainly:** this covers `api/routers/` only, and it is the
    weaker of the two guards over this boundary.
    `tests/test_boundary_guard.py` is the one that walks every module of `api/`
    as an AST and catches a hand-built boundary anywhere in the package,
    including `api/ask.py` and `api/search/query.py`, which this one cannot see.
    Both are kept: this one fires on a *router* that queries documents without
    mentioning the vault at all, which is the `/api/photos` shape, and it does
    so on a text match that survives a rewrite of the query.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent

    offenders = []
    read = 0
    considered = 0
    for path in sorted((root / "api" / "routers").glob("*.py")):
        read += 1
        if path.name in VIA_SCOPE or path.name in EXEMPT_FROM_VAULT_BOUNDARY:
            continue
        source = path.read_text()
        selects = re.search(
            r"select\(Document\)|select_from\(Document\)|Document\.library_id", source
        )
        if not selects:
            continue
        considered += 1
        if "document_clause" in source or "hidden_source_file_ids" in source:
            continue
        offenders.append(path.name)

    # A guard that silently examines nothing is worse than no guard. This one
    # had no such assertion, so reorganising `api/routers/` into subpackages
    # would have left it green forever while inspecting zero files — the same
    # failure the Phase 7 route-coverage guard had in its first version.
    assert read >= 15, (
        f"only {read} router modules were read — `api/routers/*.py` has stopped "
        "matching the tree, and this guard is now inspecting nothing"
    )
    assert considered >= 3, (
        f"only {considered} routers were found to query documents at all, which "
        "is fewer than there have ever been. Either the query spellings this "
        "guard matches have changed, or the routers moved."
    )

    assert not offenders, (
        "these routers query documents without naming the vault boundary:\n  "
        + "\n  ".join(offenders)
        + "\nApply `boundary.document_clause(user.id)`, or add the module to "
        "VIA_SCOPE with a reason."
    )


def test_the_vault_boundary_exemptions_are_all_still_real_modules() -> None:
    """An exemption naming a file that is gone is a hole held open for whichever
    module is next given that name."""
    from pathlib import Path

    routers = Path(__file__).resolve().parent.parent / "api" / "routers"
    present = {path.name for path in routers.glob("*.py")}
    orphaned = sorted(
        (set(VIA_SCOPE) | set(EXEMPT_FROM_VAULT_BOUNDARY)) - present
    )
    assert not orphaned, (
        f"these modules are exempted from the vault-boundary guard and no "
        f"longer exist: {orphaned}"
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
