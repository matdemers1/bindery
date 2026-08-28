"""Phase 7 — the leak suite (T-7.6, REQ-029, REQ-101).

> A permission bug is not an inconvenience. It is the disclosure of one
> household member's medical history to another.

So this file is written adversarially. It does not check that the happy path
works; it checks that **every** way of asking — search, facets, the command
palette, a direct URL, a blob URL, export, the audit log, the file tree —
returns nothing about a library the caller has no membership in.

The setup is three users and three libraries:

    alice   → owner of "Alice"        + contributor on "Shared"
    bob     → owner of "Bob"          + contributor on "Shared"
    ren     → reader on "Shared" only

Alice has a document Bob must never see by any route. Ren may see the shared
library but must not be able to change anything in it.

New endpoints must be added to `EVERY_READ_PATH` below. An endpoint that
returns documents and is not in that list is an untested endpoint.
"""

import uuid

import pytest
import sqlalchemy as sa

from api.db.enums import (
    ActorType,
    IngestSource,
    LibraryKind,
    MembershipRole,
    Sensitivity,
    SourceFileState,
)
from api.db.models import (
    AuditEvent,
    Correspondent,
    Document,
    DocumentTag,
    Library,
    Membership,
    Page,
    SourceFile,
    Tag,
)
from api.db.scope import resolve
from tests.conftest import PASSWORD

# The exact string that must never appear in anything Bob or Ren can see.
SECRET = "oncology consultation summary"


@pytest.fixture
async def household(session, client, user_factory):
    """Three users, three libraries, and one document that must not leak."""
    alice, alice_library = await user_factory(
        email=f"alice-{uuid.uuid4().hex[:6]}@example.test", library_name="Alice"
    )
    bob, bob_library = await user_factory(
        email=f"bob-{uuid.uuid4().hex[:6]}@example.test", library_name="Bob"
    )
    shared = Library(name="Shared", kind=LibraryKind.SHARED)
    session.add(shared)
    await session.flush()

    # Ren has no library of their own: they can look at the shared one and
    # change nothing. That is the household member this phase is really about.
    ren, _ = await user_factory(
        email=f"ren-{uuid.uuid4().hex[:6]}@example.test",
        library=shared,
        role=MembershipRole.READER,
    )
    session.add_all([
        Membership(user_id=alice.id, library_id=shared.id, role=MembershipRole.CONTRIBUTOR),
        Membership(user_id=bob.id, library_id=shared.id, role=MembershipRole.CONTRIBUTOR),
    ])

    private_file = SourceFile(
        library_id=alice_library.id, sha256=uuid.uuid4().hex * 2, byte_size=100,
        original_filename="oncology.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    shared_file = SourceFile(
        library_id=shared.id, sha256=uuid.uuid4().hex * 2, byte_size=100,
        original_filename="mortgage.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add_all([private_file, shared_file])
    await session.flush()

    session.add_all([
        Page(source_file_id=private_file.id, page_number=1,
             text=f"{SECRET} — results and follow-up plan"),
        Page(source_file_id=shared_file.id, page_number=1, text="mortgage statement"),
    ])

    clinic = Correspondent(
        library_id=alice_library.id, name="Dana-Farber Cancer Institute",
        slug=f"dfci-{uuid.uuid4().hex[:6]}",
    )
    private_tag = Tag(
        library_id=alice_library.id, name="oncology", slug=f"onc-{uuid.uuid4().hex[:6]}"
    )
    session.add_all([clinic, private_tag])
    await session.flush()

    private_document = Document(
        library_id=alice_library.id, source_file_id=private_file.id,
        page_start=1, page_end=1, title=SECRET, correspondent_id=clinic.id,
        sensitivity=Sensitivity.SENSITIVE,
    )
    shared_document = Document(
        library_id=shared.id, source_file_id=shared_file.id,
        page_start=1, page_end=1, title="Mortgage statement",
    )
    session.add_all([private_document, shared_document])
    await session.flush()
    session.add(DocumentTag(document_id=private_document.id, tag_id=private_tag.id,
                            source="human"))
    session.add(AuditEvent(entity_type="document", entity_id=private_document.id,
                           action="filed", actor_type=ActorType.AI,
                           after={"title": SECRET}))
    await session.commit()

    async def sign_in(user):
        response = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text

    return {
        "alice": alice, "bob": bob, "ren": ren,
        "alice_library": alice_library, "bob_library": bob_library, "shared": shared,
        "private_document": private_document, "shared_document": shared_document,
        "private_file": private_file, "shared_file": shared_file,
        "clinic": clinic, "private_tag": private_tag,
        "sign_in": sign_in,
    }


# --------------------------------------------------------------------------
# T-7.1 — roles
# --------------------------------------------------------------------------


async def test_roles_resolve_to_the_right_authority(session, household) -> None:
    alice = await resolve(session, household["alice"].id)
    ren = await resolve(session, household["ren"].id)

    assert set(alice.visible) == {household["alice_library"].id, household["shared"].id}
    assert set(alice.writable) == set(alice.visible)
    assert alice.is_owner(household["alice_library"].id)
    assert not alice.is_owner(household["shared"].id)

    # A reader sees the shared library and can write to nothing.
    assert set(ren.visible) == {household["shared"].id}
    assert ren.writable == ()


async def test_a_reader_cannot_write_and_is_told_why(session, household) -> None:
    from fastapi import HTTPException

    ren = await resolve(session, household["ren"].id)
    with pytest.raises(HTTPException) as raised:
        ren.require_write(household["shared"].id)
    assert raised.value.status_code == 403
    assert "read-only" in raised.value.detail


async def test_an_invisible_library_is_404_not_403(session, household) -> None:
    """403 confirms the thing exists. A probe should learn nothing."""
    from fastapi import HTTPException

    bob = await resolve(session, household["bob"].id)
    for guard in (bob.require_visible, bob.require_write):
        with pytest.raises(HTTPException) as raised:
            guard(household["alice_library"].id)
        assert raised.value.status_code == 404, "403 would confirm it exists"


# --------------------------------------------------------------------------
# T-7.2 — the scoping layer itself
# --------------------------------------------------------------------------


async def test_the_scope_query_cannot_reach_another_library(session, household) -> None:
    bob = await resolve(session, household["bob"].id)
    documents = (await session.execute(bob.documents())).scalars().all()
    titles = {document.title for document in documents}
    assert SECRET not in titles
    assert "Mortgage statement" in titles


async def test_pages_inherit_their_files_library(session, household) -> None:
    """Pages carry no library of their own — the easiest place to leak text."""
    bob = await resolve(session, household["bob"].id)
    texts = (await session.execute(bob.pages())).scalars().all()
    assert all(SECRET not in (page.text or "") for page in texts)


async def test_scope_refuses_models_it_cannot_filter(session, household) -> None:
    """Failing loudly beats silently returning everything."""
    bob = await resolve(session, household["bob"].id)
    with pytest.raises(TypeError, match="not library-scoped"):
        bob.of(AuditEvent)


# --------------------------------------------------------------------------
# T-7.6 — the leak suite proper
# --------------------------------------------------------------------------

# Every read path that can return document content. An endpoint returning
# documents and missing from this list is an endpoint nobody is testing.
EVERY_READ_PATH = [
    ("search", "/api/search?q=oncology"),
    ("archive", "/api/archive"),
    ("archive facets", "/api/archive/tree?group_by=correspondent"),
    ("file tree", "/api/tree"),
    ("documents", "/api/documents"),
    ("review queue", "/api/review"),
    ("vital", "/api/vital"),
    ("audit log", "/api/audit"),
    ("correspondents", "/api/correspondents"),
    ("taxonomy health", "/api/taxonomy/health"),
    ("duplicates", "/api/duplicates"),
    ("pipeline", "/api/pipeline"),
    ("libraries", "/api/libraries"),
    # Added by the route-coverage guard below, which found them untested.
    ("source files", "/api/source-files"),
    ("imports", "/api/imports"),
    ("assets", "/api/assets"),
    ("rules", "/api/rules"),
    ("household libraries", "/api/household/libraries"),
]


@pytest.mark.parametrize("label,path", EVERY_READ_PATH, ids=[p[0] for p in EVERY_READ_PATH])
async def test_no_read_path_leaks_another_library(client, household, label, path) -> None:
    """The whole point of the phase, asserted one endpoint at a time."""
    await household["sign_in"](household["bob"])
    response = await client.get(path)
    assert response.status_code in (200, 404), f"{label}: {response.status_code} {response.text}"
    if response.status_code == 200:
        body = response.text.lower()
        assert SECRET not in body, f"{label} leaked Alice's document to Bob"
        assert "dana-farber" not in body, f"{label} leaked Alice's correspondent to Bob"
        assert "oncology.pdf" not in body, f"{label} leaked Alice's filename to Bob"


async def test_a_direct_document_url_is_not_a_way_in(client, household) -> None:
    await household["sign_in"](household["bob"])
    response = await client.get(f"/api/documents/{household['private_document'].id}")
    assert response.status_code == 404, "knowing the id must not be enough"
    assert SECRET not in response.text


async def test_a_blob_url_is_not_a_way_in(client, household) -> None:
    """The file endpoints serve bytes. They are the highest-consequence leak."""
    await household["sign_in"](household["bob"])
    file_id = household["private_file"].id
    for path in (
        f"/api/files/{file_id}/pdf",
        f"/api/files/{file_id}/pages/1/render",
        f"/api/files/{file_id}/pages/1/thumb",
        f"/api/documents/{household['private_document'].id}/pdf",
    ):
        response = await client.get(path)
        assert response.status_code == 404, f"{path} served another library's bytes"


async def test_the_command_palette_is_not_a_way_in(client, household) -> None:
    await household["sign_in"](household["bob"])
    response = await client.get("/api/search", params={"q": "oncology", "limit": 50})
    assert response.status_code == 200
    assert SECRET not in response.text


async def test_export_only_carries_what_the_caller_can_see(
    client, session, household, tmp_path
) -> None:
    from api.export import archive_export

    bob = await resolve(session, household["bob"].id)
    result = await archive_export.full_export(
        session, list(bob.visible), destination=tmp_path / "bob-export"
    )
    index = (result.path / "index.html").read_text()
    payload = (result.path / "documents.json").read_text()
    assert SECRET not in index and SECRET not in payload
    assert "Mortgage statement" in index


async def test_the_audit_log_does_not_leak_another_librarys_history(
    client, household
) -> None:
    """The `after` blob of an audit event is document content by another name."""
    await household["sign_in"](household["bob"])
    response = await client.get("/api/audit")
    assert response.status_code == 200, response.text
    assert SECRET not in response.text

    # Nor by asking for the event directly.
    direct = await client.get(
        "/api/audit", params={"entity_id": str(household["private_document"].id)}
    )
    assert direct.json()["events"] == []


async def test_alice_still_sees_her_own_archive(client, household) -> None:
    """A boundary that hides everything is not a boundary, it is an outage."""
    await household["sign_in"](household["alice"])

    search = await client.get("/api/search", params={"q": "oncology"})
    assert SECRET in search.text

    audit = await client.get(
        "/api/audit", params={"entity_id": str(household["private_document"].id)}
    )
    assert len(audit.json()["events"]) == 1


async def test_the_shared_library_is_shared(client, household) -> None:
    await household["sign_in"](household["bob"])
    response = await client.get("/api/search", params={"q": "mortgage"})
    assert response.status_code == 200
    assert "mortgage" in response.text.lower()


# --------------------------------------------------------------------------
# T-7.2 — the assertion that keeps the suite honest (REQ-101)
# --------------------------------------------------------------------------

# GET routes under /api that cannot return library-scoped data, and so do not
# need a leak-suite entry. Every addition here is a claim you are making; the
# test below verifies the claim is at least plausible by requiring a reason.
NOT_LIBRARY_SCOPED = {
    "/api/health": "liveness only",
    "/api/auth/me": "the caller's own identity",
    "/api/settings": "instance settings; secrets are masked, never returned",
    "/api/forms": "the known-form registry is global, not per library",
    "/api/shelves": "already covered through /api/archive",
    "/api/openapi.json": "the schema document; contains no data",
}


def test_every_document_returning_route_is_in_the_leak_suite() -> None:
    """A new endpoint must not be able to quietly skip this file.

    The audit-log endpoint shipped in Phase 6 returning every household
    member's history, and it looked correct: it called the permission helper
    and then threw the answer away. Nothing static would have caught that. What
    catches it is exercising every route with a user who should see nothing —
    so the failure mode to defend against is a route that never gets exercised.
    """
    from api.main import app

    covered = {path.split("?")[0] for _, path in EVERY_READ_PATH}
    uncovered: list[str] = []
    considered = 0

    # The OpenAPI document rather than `app.routes`: included routers nest
    # their routes behind an opaque wrapper, and a naive walk of `app.routes`
    # sees four schema endpoints and nothing else — which is how this guard
    # first passed while examining zero routes. The schema is the public,
    # stable enumeration of what this app actually exposes.
    for path, operations in app.openapi()["paths"].items():
        if "get" not in operations or not path.startswith("/api"):
            continue
        # Routes with path parameters are exercised individually above (direct
        # document URLs, blob URLs) rather than by the parametrised sweep.
        if "{" in path or path in NOT_LIBRARY_SCOPED or path.startswith("/api/docs"):
            continue
        considered += 1
        if path not in covered:
            uncovered.append(path)

    # A guard that silently examines nothing is worse than no guard: it reads
    # green forever while the route table grows underneath it.
    assert considered >= len(EVERY_READ_PATH), (
        f"only {considered} routes were examined — the route introspection has "
        "stopped matching this version of FastAPI"
    )
    assert not uncovered, (
        "these GET routes can return library-scoped data and are not exercised "
        "by the leak suite. Add them to EVERY_READ_PATH, or to "
        "NOT_LIBRARY_SCOPED with a reason:\n  " + "\n  ".join(sorted(uncovered))
    )


# --------------------------------------------------------------------------
# T-7.3 — audited moves (REQ-102)
# --------------------------------------------------------------------------


async def test_moving_a_file_moves_every_document_in_it(session, client, household) -> None:
    """The unit is the file, because the composite FK makes it the unit."""
    await household["sign_in"](household["alice"])
    file_id = household["private_file"].id

    response = await client.post(
        f"/api/source-files/{file_id}/move",
        json={"to_library_id": str(household["shared"].id)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_count"] == 1
    assert body["to_library_id"] == str(household["shared"].id)

    await session.commit()
    moved = await session.get(SourceFile, file_id)
    await session.refresh(moved)
    assert moved.library_id == household["shared"].id

    document = await session.get(Document, household["private_document"].id)
    await session.refresh(document)
    assert document.library_id == household["shared"].id, (
        "a document must never be left in a library its file is not in"
    )


async def test_a_move_previews_what_it_will_cost(client, household) -> None:
    """Tags and correspondents belong to a library and do not travel."""
    await household["sign_in"](household["alice"])
    response = await client.post(
        f"/api/source-files/{household['private_file'].id}/move/preview",
        json={"to_library_id": str(household["shared"].id)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["loses_metadata"] is True
    assert body["cleared_tags"] == ["oncology"]
    assert body["cleared_correspondents"] == ["Dana-Farber Cancer Institute"]


async def test_a_preview_changes_nothing(session, client, household) -> None:
    await household["sign_in"](household["alice"])
    await client.post(
        f"/api/source-files/{household['private_file'].id}/move/preview",
        json={"to_library_id": str(household["shared"].id)},
    )
    await session.commit()
    unchanged = await session.get(SourceFile, household["private_file"].id)
    await session.refresh(unchanged)
    assert unchanged.library_id == household["alice_library"].id


async def test_a_move_is_audited_with_before_and_after(session, client, household) -> None:
    await household["sign_in"](household["alice"])
    await client.post(
        f"/api/source-files/{household['private_file'].id}/move",
        json={"to_library_id": str(household["shared"].id)},
    )
    event = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == household["private_file"].id,
                AuditEvent.action == "moved_library",
            )
        )
    ).scalar_one()
    assert event.before["library_id"] == str(household["alice_library"].id)
    assert event.after["to_library_id"] == str(household["shared"].id)
    # What was lost is recorded, not merely counted.
    assert event.before["tags"] == ["oncology"]


async def test_cleared_tags_are_revoked_not_deleted(session, client, household) -> None:
    """REQ-090: the history of what this was tagged with survives the move."""
    await household["sign_in"](household["alice"])
    await client.post(
        f"/api/source-files/{household['private_file'].id}/move",
        json={"to_library_id": str(household["shared"].id)},
    )
    await session.commit()
    link = (
        await session.execute(
            sa.select(DocumentTag).where(
                DocumentTag.document_id == household["private_document"].id
            )
        )
    ).scalar_one()
    assert link.removed_at is not None, "the link is revoked"
    assert link.tag_id == household["private_tag"].id, "the row still exists"


async def test_a_reader_cannot_move_anything_in(client, household) -> None:
    await household["sign_in"](household["ren"])
    response = await client.post(
        f"/api/source-files/{household['shared_file'].id}/move",
        json={"to_library_id": str(household["shared"].id)},
    )
    assert response.status_code == 403


async def test_you_cannot_move_a_file_you_cannot_see(client, household) -> None:
    await household["sign_in"](household["bob"])
    response = await client.post(
        f"/api/source-files/{household['private_file'].id}/move",
        json={"to_library_id": str(household["bob_library"].id)},
    )
    assert response.status_code == 404, "403 would confirm the file exists"


async def test_you_cannot_move_a_file_into_a_library_you_cannot_write(
    client, household
) -> None:
    """Otherwise a move is a way to put documents where you cannot be audited."""
    await household["sign_in"](household["bob"])
    response = await client.post(
        f"/api/source-files/{household['shared_file'].id}/move",
        json={"to_library_id": str(household["alice_library"].id)},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# T-7.7 — the libraries screen
# --------------------------------------------------------------------------


async def test_the_libraries_screen_shows_only_your_libraries(client, household) -> None:
    await household["sign_in"](household["bob"])
    response = await client.get("/api/household/libraries")
    assert response.status_code == 200, response.text
    names = {entry["name"] for entry in response.json()}
    assert names == {"Bob", "Shared"}
    assert "Alice" not in names


async def test_the_screen_says_what_you_may_do_in_each_library(client, household) -> None:
    await household["sign_in"](household["ren"])
    entries = (await client.get("/api/household/libraries")).json()
    assert [entry["your_role"] for entry in entries] == ["reader"]
    # Ren can see who else is in the shared library — that is not a leak, it is
    # the point of a shared library.
    assert len(entries[0]["members"]) == 3


async def test_only_an_owner_can_change_membership(client, household) -> None:
    await household["sign_in"](household["bob"])
    response = await client.put(
        f"/api/household/libraries/{household['shared'].id}/members",
        json={"email": household["ren"].email, "role": "contributor"},
    )
    assert response.status_code == 403, "a contributor cannot grant roles"


async def test_membership_changes_are_audited(session, client, household) -> None:
    await household["sign_in"](household["alice"])
    response = await client.put(
        f"/api/household/libraries/{household['alice_library'].id}/members",
        json={"email": household["bob"].email, "role": "reader"},
    )
    assert response.status_code == 200, response.text

    event = (
        await session.execute(
            sa.select(AuditEvent)
            .where(
                AuditEvent.entity_id == household["alice_library"].id,
                AuditEvent.action == "membership_changed",
            )
        )
    ).scalar_one()
    assert event.before["role"] is None
    assert event.after["role"] == "reader"


async def test_the_last_owner_cannot_demote_themselves(client, household) -> None:
    """A library with no owner is a library nobody can ever fix."""
    await household["sign_in"](household["alice"])
    response = await client.put(
        f"/api/household/libraries/{household['alice_library'].id}/members",
        json={"email": household["alice"].email, "role": "reader"},
    )
    assert response.status_code == 409
    assert "only owner" in response.text


# --------------------------------------------------------------------------
# T-7.4 / T-7.5 — routing in, and the classification path
# --------------------------------------------------------------------------


async def test_candidate_taxonomy_never_crosses_a_library(session, household) -> None:
    """REQ-048, the input half. Filtering the prompt is the first defence; the
    re-validation of returned ids in `worker/classify/resolve.py` is the second."""
    from worker.classify.candidates import build

    candidates = await build(
        session, household["shared_document"], [household["shared"].id]
    )
    names = {
        candidate.name
        for group in (candidates.correspondents, candidates.document_types, candidates.tags)
        for candidate in group
    }
    assert "Dana-Farber Cancer Institute" not in names
    assert "oncology" not in names
