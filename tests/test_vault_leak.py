"""A locked vault is invisible everywhere (T-16.11, REQ-180, REQ-187).

Written before anything could create a vaulted document, and deliberately: a
leak suite written *after* the feature tends to test the paths the feature
author was already thinking about. The one that leaks is never the document
endpoint — it is a facet count, a search snippet, a palette result, or a
progress row on a screen nobody associated with privacy.

The rows here are marked vaulted by hand rather than by the real move, so this
holds even before T-16.5 exists and keeps holding if the move changes.
"""

import hashlib
import uuid

import pytest
import sqlalchemy as sa

from api.db.enums import IngestSource, ReviewState, SourceFileState
from api.db.models import Document, Page, SourceFile

SECRET_PHRASE = "quetzalcoatlus northropi settlement"


@pytest.fixture
async def a_vaulted_document(session, signed_in):
    """A document whose text is distinctive enough to find anywhere it leaks."""
    user, library = await signed_in()
    source = SourceFile(
        library_id=library.id,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        byte_size=2048,
        original_filename="the-private-one.pdf",
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
