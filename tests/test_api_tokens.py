"""Phase 8 — scoped API tokens (T-8.5, REQ-107).

A token is a credential that will end up in a shell script, a scanner config,
or a cron file. So the questions it has to answer well are: what can it do, what
can it reach, and how fast does that stop being true.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from api import tokens
from api.db.enums import (
    IngestSource,
    LibraryKind,
    MembershipRole,
    ReviewState,
    SourceFileState,
)
from api.db.models import ApiToken, Document, Library, Membership, Page, SourceFile


@pytest.fixture
async def owner(session, signed_in):
    user, library = await signed_in()
    return user, library


async def test_the_secret_is_returned_once_and_only_the_hash_is_stored(
    session, client, owner
) -> None:
    _, library = owner
    response = await client.post(
        "/api/tokens",
        json={"name": "scanner", "scopes": ["upload"], "library_ids": [str(library.id)]},
    )
    assert response.status_code == 201, response.text
    secret = response.json()["secret"]
    assert secret.startswith(tokens.PREFIX)

    row = (
        await session.execute(sa.select(ApiToken).where(ApiToken.name == "scanner"))
    ).scalar_one()
    assert row.token_hash != secret
    assert row.token_hash == tokens.hash_token(secret)

    # And it is never handed back again.
    listed = (await client.get("/api/tokens")).json()
    assert all("secret" not in entry for entry in listed)


async def test_a_token_authenticates_a_request(client, owner) -> None:
    _, _library = owner
    secret = (
        await client.post(
            "/api/tokens", json={"name": "reader", "scopes": ["read"]}
        )
    ).json()["secret"]

    # A clean client with no cookie: the token is doing all the work.
    client.cookies.clear()
    response = await client.get("/api/documents", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200, response.text


async def test_a_token_without_the_scope_is_refused(session, client, owner) -> None:
    """Verification step 5: an out-of-scope request is refused."""
    user, library = owner
    issued = await tokens.issue(
        session, user_id=user.id, name="read only", scopes=["read"], library_ids=[library.id]
    )
    await session.commit()

    identity = await tokens.verify(session, issued.secret)
    assert identity is not None
    assert identity.allows("read")
    assert not identity.allows("upload")
    assert not identity.allows("export")


async def test_admin_implies_the_rest(session, owner) -> None:
    user, library = owner
    issued = await tokens.issue(
        session, user_id=user.id, name="everything", scopes=["admin"],
        library_ids=[library.id],
    )
    await session.commit()
    identity = await tokens.verify(session, issued.secret)
    assert identity is not None
    assert identity.allows("upload") and identity.allows("export") and identity.allows("read")


async def test_an_unknown_scope_is_refused_not_ignored(session, owner) -> None:
    """A scope that silently does nothing is a permission you think you granted."""
    user, library = owner
    with pytest.raises(ValueError, match="unknown scope"):
        await tokens.issue(
            session, user_id=user.id, name="typo", scopes=["raed"], library_ids=[library.id]
        )


async def test_a_token_cannot_reach_a_library_its_creator_cannot(
    session, owner, signed_in
) -> None:
    user, _ = owner
    _, elsewhere = await signed_in()
    with pytest.raises(ValueError, match="cannot reach"):
        await tokens.issue(
            session, user_id=user.id, name="overreach", scopes=["read"],
            library_ids=[elsewhere.id],
        )


async def test_losing_a_membership_immediately_narrows_the_token(
    session, owner, user_factory
) -> None:
    """Re-intersected on every request, so revoking access needs no token hunt."""
    user, library = owner
    shared = Library(name="Shared", kind=LibraryKind.SHARED)
    session.add(shared)
    await session.flush()
    membership = Membership(
        user_id=user.id, library_id=shared.id, role=MembershipRole.CONTRIBUTOR
    )
    session.add(membership)
    await session.commit()

    issued = await tokens.issue(
        session, user_id=user.id, name="both", scopes=["read"],
        library_ids=[library.id, shared.id],
    )
    await session.commit()

    identity = await tokens.verify(session, issued.secret)
    assert set(identity.library_ids) == {library.id, shared.id}

    await session.delete(membership)
    await session.commit()

    narrowed = await tokens.verify(session, issued.secret)
    assert set(narrowed.library_ids) == {library.id}


async def test_a_revoked_token_stops_working_but_stays_on_the_record(
    session, client, owner
) -> None:
    _, _library = owner
    created = (
        await client.post("/api/tokens", json={"name": "temp", "scopes": ["read"]})
    ).json()
    secret = created["secret"]

    assert await tokens.verify(session, secret) is not None

    revoked = await client.post(f"/api/tokens/{created['id']}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    assert await tokens.verify(session, secret) is None, "a revoked token is dead"
    # REQ-090: it is still listed, because what existed is part of the record.
    listed = (await client.get("/api/tokens")).json()
    assert any(entry["id"] == created["id"] for entry in listed)


async def test_an_expired_token_stops_working(session, owner) -> None:
    user, library = owner
    issued = await tokens.issue(
        session, user_id=user.id, name="short", scopes=["read"],
        library_ids=[library.id], expires_in_days=1,
    )
    issued.record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    assert await tokens.verify(session, issued.secret) is None


async def test_a_forged_token_is_rejected(session) -> None:
    assert await tokens.verify(session, "bnd_" + "a" * 40) is None
    assert await tokens.verify(session, "not-a-bindery-token") is None


async def test_a_token_is_scoped_to_its_libraries_in_practice(
    session, client, owner, signed_in
) -> None:
    """The leak boundary applies to tokens exactly as it does to sessions."""
    user, _library = owner
    shared = Library(name="Shared", kind=LibraryKind.SHARED)
    session.add(shared)
    await session.flush()
    session.add(
        Membership(user_id=user.id, library_id=shared.id, role=MembershipRole.CONTRIBUTOR)
    )
    await session.commit()

    secret = (
        await client.post(
            "/api/tokens",
            json={"name": "narrow", "scopes": ["read"], "library_ids": [str(shared.id)]},
        )
    ).json()["secret"]

    client.cookies.clear()
    response = await client.get(
        "/api/household/libraries", headers={"Authorization": f"Bearer {secret}"}
    )
    assert response.status_code == 200, response.text
    names = {entry["name"] for entry in response.json()}
    assert names == {"Shared"}, "the token sees only what it was scoped to"


# ---------------------------------------------------------------------------
# What a token may reach (CR-004, CR-034)
#
# A token is a subset of the person who issued it in two directions: the
# libraries it names, and the things it was granted the capability to do. Both
# used to hold only inside `current_scope`, which one router of twenty asked
# for — so the assertions below are deliberately made against routers that
# never mention a token.
# ---------------------------------------------------------------------------

ELSEWHERE = "pyrenean ibex conservatorship"
AUDITED_TITLE = "Kessington deed amendment"


async def _a_document_in(session, library, *, phrase: str) -> Document:
    """A findable document, so a leak shows up in a payload rather than a count."""
    source = SourceFile(
        library_id=library.id,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        byte_size=1024,
        original_filename="elsewhere.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    session.add(
        Page(source_file_id=source.id, page_number=1, text=f"{phrase} appears here")
    )
    document = Document(
        library_id=library.id,
        source_file_id=source.id,
        page_start=1,
        page_end=1,
        title=f"The other library — {phrase}",
        review_state=ReviewState.FILED,
    )
    session.add(document)
    await session.commit()
    return document


async def test_a_narrow_token_stays_narrow_where_nobody_asked(session, client, owner) -> None:
    """The narrowing belongs to the repository, not to each router.

    `/api/documents`, `/api/libraries` and `/api/search` take `current_user` and
    ask for "the libraries this user can see" — which is what nineteen of the
    twenty routers do, and why a token scoped to one library used to read every
    library its creator could.
    """
    user, personal = owner
    document = await _a_document_in(session, personal, phrase=ELSEWHERE)

    shared = Library(name="Shared", kind=LibraryKind.SHARED)
    session.add(shared)
    await session.flush()
    session.add(
        Membership(user_id=user.id, library_id=shared.id, role=MembershipRole.CONTRIBUTOR)
    )
    await session.commit()

    # The person can see it; that is what makes the token's silence meaningful.
    mine = await client.get("/api/search", params={"q": ELSEWHERE})
    assert mine.json()["total"] == 1, mine.text

    # An audited mutation in the library the token is *not* given. Without one
    # the audit assertion below passes against an empty trail and proves
    # nothing — the shape of vacuous test this review has been finding.
    edited = await client.patch(
        f"/api/documents/{document.id}", json={"title": AUDITED_TITLE}
    )
    assert edited.status_code == 200, edited.text
    mine_audit = await client.get("/api/audit")
    assert AUDITED_TITLE in mine_audit.text, "the fixture never recorded an audit event"

    secret = (
        await client.post(
            "/api/tokens",
            json={"name": "narrow", "scopes": ["read"], "library_ids": [str(shared.id)]},
        )
    ).json()["secret"]

    client.cookies.clear()
    headers = {"Authorization": f"Bearer {secret}"}

    documents = await client.get("/api/documents", headers=headers)
    assert documents.status_code == 200, documents.text
    assert str(document.id) not in documents.text
    assert ELSEWHERE not in documents.text

    libraries = await client.get("/api/libraries", headers=headers)
    assert {entry["name"] for entry in libraries.json()} == {"Shared"}

    found = await client.get("/api/search", params={"q": ELSEWHERE}, headers=headers)
    assert found.json()["total"] == 0, "the token searched a library it was not given"

    # `/api/audit` was the twentieth router: the one call site that reached
    # `scope.resolve` directly instead of going through the repository, so it
    # answered for every library the *person* belongs to. Its `before`/`after`
    # blobs are document content by another name, which makes it the worst
    # possible one to have missed.
    audit = await client.get("/api/audit", headers=headers)
    assert audit.status_code == 200, audit.text
    assert AUDITED_TITLE not in audit.text, (
        "the token read the audit trail of a library it was not given"
    )
    assert str(document.id) not in audit.text


async def test_a_read_only_token_cannot_change_anything(client, owner) -> None:
    """`read` is a capability, not a label on a screen."""
    secret = (
        await client.post("/api/tokens", json={"name": "reader", "scopes": ["read"]})
    ).json()["secret"]

    client.cookies.clear()
    headers = {"Authorization": f"Bearer {secret}"}

    # Minting a second token is the escalation that would make every other
    # scope decorative — a read token that can issue an admin one is unbounded.
    minted = await client.post(
        "/api/tokens", json={"name": "wider", "scopes": ["admin"]}, headers=headers
    )
    assert minted.status_code == 403, minted.text

    edited = await client.patch(
        f"/api/documents/{uuid.uuid4()}", json={"title": "renamed"}, headers=headers
    )
    assert edited.status_code == 403, "refused before the route ran, so not a 404"

    revoked = await client.post(f"/api/tokens/{uuid.uuid4()}/revoke", headers=headers)
    assert revoked.status_code == 403, revoked.text


async def test_an_upload_token_is_not_a_read_token(client, owner) -> None:
    """The scanner's credential is not a way to read the archive."""
    secret = (
        await client.post("/api/tokens", json={"name": "scanner", "scopes": ["upload"]})
    ).json()["secret"]

    client.cookies.clear()
    response = await client.get(
        "/api/documents", headers={"Authorization": f"Bearer {secret}"}
    )
    assert response.status_code == 403, response.text
    assert "read" in response.json()["detail"]


async def test_no_token_reaches_the_vault(session, client, owner) -> None:
    """ADR-012, and this router's own docstring.

    Unlocking is something a person did at a keyboard with a PIN; a long-lived
    bearer token is the opposite. Asserted with the widest token there is, and
    against an *unlocked* vault — the fifteen minutes after an unlock is the
    window in which the routes would otherwise hand over plaintext.
    """
    from api.vault import service
    from api.vault.session import sessions

    user, _library = owner
    await service.create(session, user.id, "a-long-enough-passphrase", "481516")
    await session.commit()
    unlocked = await client.post("/api/vault/unlock", json={"pin": "481516"})
    assert unlocked.status_code == 200, unlocked.text

    secret = (
        await client.post("/api/tokens", json={"name": "everything", "scopes": ["admin"]})
    ).json()["secret"]

    try:
        client.cookies.clear()
        headers = {"Authorization": f"Bearer {secret}"}
        item = uuid.uuid4()
        for method, path in (
            ("get", "/api/vault"),
            ("get", "/api/vault/items"),
            ("get", "/api/vault/search"),
            ("get", f"/api/vault/items/{item}/original"),
            ("post", "/api/vault/unlock"),
            ("post", "/api/vault/lock"),
            ("post", f"/api/vault/items/{item}"),
            ("delete", f"/api/vault/items/{item}"),
        ):
            response = await getattr(client, method)(path, headers=headers)
            assert response.status_code == 403, f"{method.upper()} {path} → {response.text}"
    finally:
        sessions.lock(user.id)


async def test_only_an_admin_scoped_token_reaches_account_administration(
    session, client, owner
) -> None:
    """`admin` is the scope that means "everything the creating user can do".

    Account administration never returns a document (ADR-009), but who holds an
    account, and inviting or suspending one, is not what a scanner's credential
    or a nightly export script is for.
    """
    user, _library = owner
    user.is_admin = True
    await session.commit()

    reader = (
        await client.post("/api/tokens", json={"name": "reader", "scopes": ["read"]})
    ).json()["secret"]
    everything = (
        await client.post("/api/tokens", json={"name": "everything", "scopes": ["admin"]})
    ).json()["secret"]

    client.cookies.clear()
    refused = await client.get(
        "/api/admin/accounts", headers={"Authorization": f"Bearer {reader}"}
    )
    assert refused.status_code == 403, refused.text

    allowed = await client.get(
        "/api/admin/accounts", headers={"Authorization": f"Bearer {everything}"}
    )
    assert allowed.status_code == 200, allowed.text


def test_the_refusals_still_name_the_routes_they_guard() -> None:
    """A path prefix is a guard only while it matches the routes it guards."""
    from api.auth.dependencies import ADMIN_PATH
    from api.main import app
    from api.routers.accounts import require_admin

    administered = []
    for included in app.routes:
        router = getattr(included, "original_router", None)
        for route in getattr(router, "routes", []):
            dependant = getattr(route, "dependant", None)
            if dependant is None:
                continue
            if any(dependency.call is require_admin for dependency in dependant.dependencies):
                administered.append("/api" + route.path)

    assert administered, "no route requires an administrator; the guard found nothing"
    assert all(path.startswith(ADMIN_PATH) for path in administered), (
        "an administration route moved out from under the prefix the token "
        f"refusal watches: {sorted(set(administered))}"
    )


def test_the_vault_refusal_still_names_the_vault_routes() -> None:
    """A path prefix is a guard only while it matches the routes it guards."""
    from api.auth.dependencies import VAULT_PATH
    from api.main import app

    vault_routes = [
        path
        for path, operations in app.openapi()["paths"].items()
        if any(
            "vault" in operation.get("tags", [])
            for operation in operations.values()
            if isinstance(operation, dict)
        )
    ]
    assert vault_routes, "nothing is tagged vault; the refusal is pointing at nothing"
    assert all(path.startswith(VAULT_PATH) for path in vault_routes), (
        "the vault router moved and the token refusal did not follow it"
    )
