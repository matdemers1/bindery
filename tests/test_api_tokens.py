"""Phase 8 — scoped API tokens (T-8.5, REQ-107).

A token is a credential that will end up in a shell script, a scanner config,
or a cron file. So the questions it has to answer well are: what can it do, what
can it reach, and how fast does that stop being true.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from api import tokens
from api.db.enums import LibraryKind, MembershipRole
from api.db.models import ApiToken, Library, Membership


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
