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
    ("health panel", "/api/health/panel"),
    ("api tokens", "/api/tokens"),
    ("pending AI review", "/api/pipeline/reclassify/pending"),
    ("logs", "/api/logs"),
    ("pipeline files", "/api/pipeline/files"),
    ("photo wall", "/api/photos"),
    # The edit form's pickers (Phase 17). A tag list that reaches across
    # libraries would let one person enumerate another's taxonomy — which is
    # not document content, but it is a map of what somebody keeps.
    ("tags", "/api/tags"),
    ("document types", "/api/document-types"),
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
    "/api/account": "the caller's own account and its own storage total",
    "/api/version": "build metadata; deliberately unauthenticated (REQ-152)",
    "/api/account/totp": "the caller's own two-factor state",
    # These two ARE cross-account, deliberately, and are covered by
    # test_an_administrator_sees_accounts_and_no_documents below — which is the
    # stronger check, because it asserts what they may *not* contain.
    "/api/admin/accounts": "admin: account metadata only, asserted separately (ADR-009)",
    "/api/admin/invitations": "admin: invitations only, no library data",
    # Archive-wide operational state: when a copy last reached S3, and why one
    # failed. No document, page, title or content address appears in it — which
    # is asserted separately below rather than assumed, because the version of
    # this that shipped first *did* carry blob hashes in its failure strings.
    "/api/offsite": "replication state; asserted to contain no archive data (ADR-009)",
    # User-scoped rather than library-scoped, and asserted far more strictly in
    # tests/test_vault_leak.py: a locked vault returns nothing anywhere, and an
    # unlocked one is the caller's own by construction — there is no vault
    # belonging to a library or shared with anyone.
    "/api/vault": "the caller's own vault state; says nothing while locked (ADR-012)",
    "/api/vault/items": "the caller's own vault; 423 while locked",
    "/api/vault/search": "the caller's own vault; 423 while locked",
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



# --------------------------------------------------------------------------
# ADR-009: an administrator administers accounts, not documents
# --------------------------------------------------------------------------


async def test_an_administrator_cannot_read_another_library(
    session, client, user_factory, signed_in
) -> None:
    """REQ-143. The promise that makes this shareable with family is that the
    operator cannot read what they are given. It has to be a test, not a
    sentence: an admin read path added "just to help" is how this ends badly.
    """
    admin, _own = await signed_in()
    admin.is_admin = True
    await session.commit()

    for _label, path in EVERY_READ_PATH:
        if path.split("?")[0] in NOT_LIBRARY_SCOPED:
            continue
        response = await client.get(path)
        body = response.text
        assert SECRET not in body, (
            f"{path} leaked another library's document to an administrator"
        )


async def test_visible_library_ids_has_no_admin_branch() -> None:
    """The boundary is one function, and the failure mode is someone adding a
    kindly `if user.is_admin` to it. Nothing else in the codebase would notice.
    """
    import inspect

    from api.db import repository

    source = inspect.getsource(repository.visible_library_ids)
    assert "is_admin" not in source
    assert "admin" not in source.lower(), (
        "ADR-009: administration is about accounts, and this function is about "
        "documents. If that is changing, the ADR changes first."
    )


async def test_the_admin_account_list_carries_no_document_data(
    session, client, signed_in
) -> None:
    """Counts, states and timestamps. Never a title, never a filename."""
    admin, _ = await signed_in()
    admin.is_admin = True
    await session.commit()

    response = await client.get("/api/admin/accounts")
    assert response.status_code == 200
    body = response.text
    assert SECRET not in body
    for field in ("title", "original_filename", "summary", "snippet"):
        assert f'"{field}"' not in body


async def test_a_non_admin_gets_404_from_the_admin_routes(client, signed_in) -> None:
    """404 rather than 403: a 403 confirms the route exists and that somebody
    somewhere is an administrator."""
    await signed_in()
    for path in ("/api/admin/accounts", "/api/admin/invitations"):
        response = await client.get(path)
        assert response.status_code == 404, path


async def test_uploading_a_file_someone_else_holds_does_not_reveal_them(
    session, client, signed_in, user_factory
) -> None:
    """A cross-tenant leak found before there was a second account to leak to.

    `sha256` was globally unique and the dedup lookup matched globally, so
    uploading bytes another household already held returned *their*
    `source_file` — filename, library id and all. Two failures in one: it
    answered "does anyone else have this document", and your own copy was never
    filed in your own library while the response said it worked.
    """
    import io

    from api.db.enums import IngestSource
    from api.db.models import SourceFile

    _other_user, other_library = await user_factory()
    shared_bytes = b"%PDF-1.4\nthe same bytes in two households\n"
    import hashlib

    sha = hashlib.sha256(shared_bytes).hexdigest()
    session.add(
        SourceFile(
            library_id=other_library.id, sha256=sha, byte_size=len(shared_bytes),
            original_filename="their-private-name.pdf",
            ingest_source=IngestSource.WEB_UPLOAD,
        )
    )
    await session.commit()

    _me, my_library = await signed_in()
    response = await client.post(
        "/api/upload",
        data={"library_id": str(my_library.id)},
        files={"file": ("mine.pdf", io.BytesIO(shared_bytes), "application/pdf")},
    )

    assert response.status_code == 201, "my upload is filed, not swallowed"
    body = response.json()
    assert "their-private-name.pdf" not in response.text
    assert body["source_file"]["library_id"] == str(my_library.id)
    assert body["duplicate"] is False


async def test_the_same_file_twice_in_one_library_is_still_deduplicated(
    session, client, signed_in
) -> None:
    """Scoping the lookup must not cost the deduplication it was there for."""
    import io

    _me, my_library = await signed_in()
    payload = b"%PDF-1.4\nuploaded twice by the same person\n"

    first = await client.post(
        "/api/upload",
        data={"library_id": str(my_library.id)},
        files={"file": ("a.pdf", io.BytesIO(payload), "application/pdf")},
    )
    second = await client.post(
        "/api/upload",
        data={"library_id": str(my_library.id)},
        files={"file": ("a-again.pdf", io.BytesIO(payload), "application/pdf")},
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["source_file"]["id"] == first.json()["source_file"]["id"]


async def test_the_offsite_panel_reveals_nothing_about_the_archive(
    client, session, signed_in
) -> None:
    """Every household member can open the Trust screen, so this one is shared.

    Which makes a content address on it an existence oracle: anyone holding a
    copy of a file could confirm that somebody here holds it too. That is the
    cross-tenant leak migration 0017 closed through per-library dedup, and the
    first version of this panel reopened it through a different door by putting
    blob hashes in its failure strings.
    """
    import re

    from api.db.models import OffsiteRun

    await signed_in()
    session.add(
        OffsiteRun(
            kind="daily", state="failed", trigger="schedule",
            detail="2 blob(s) failed to upload — the dump was not sent",
            failures=["2 blobs: Denied."],
        )
    )
    await session.commit()

    body = (await client.get("/api/offsite")).text
    assert "Denied" in body, "the reason must survive; it is the diagnosis"

    # Row ids are UUIDs and are fine — they are synthetic, not derived from any
    # document. They also contain 12-character hex runs, which is exactly the
    # length a sha256 prefix was, so they have to be removed before looking or
    # this assertion fires on its own identifiers.
    without_uuids = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "", body
    )
    assert not re.search(r"\b[0-9a-f]{12,}\b", without_uuids), (
        "a content address reached the shared Trust screen"
    )
