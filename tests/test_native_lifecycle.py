"""The D3 App contract's account lifecycle (BND-T-23.2, BND-T-23.3, BND-ADR-015): accepting an
invite natively, and deleting an account from the app — the request, the grace period, the way
back, and the purge on a test clock."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from httpx import AsyncClient

from api import account_purge, accounts, tokens
from api.auth import service
from api.auth.totp import _code_for_step, current_step
from api.db.enums import (
    IngestSource,
    LibraryKind,
    MembershipRole,
    ReviewState,
    SourceFileState,
)
from api.db.models import (
    ApiToken,
    AppUser,
    AuditEvent,
    Document,
    Invitation,
    Library,
    Membership,
    Page,
    RefreshToken,
    RelayRegistration,
    SourceFile,
    Tag,
    Vault,
    VaultItem,
)
from tests.conftest import PASSWORD
from tests.test_native_contract import (
    DEVICE,
    problem_type,
    sign_in,
    with_authenticator,
)

DELETE = "/api/auth/native/delete-account"
INVITE = "/api/auth/native/invite"


def own_address() -> dict[str, str]:
    """A fresh address per test that fails on purpose, so its failures throttle nobody else."""
    return {"CF-Connecting-IP": f"198.51.100.{uuid.uuid4().int % 250 + 1}"}


def bearer(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['accessToken']}"}


async def _invite(session, admin: AppUser, **kwargs) -> str:
    issued = await accounts.invite(
        session,
        email=f"invited-{uuid.uuid4().hex[:8]}@example.test",
        library_name=kwargs.pop("library_name", "Guest papers"),
        created_by=admin,
        **kwargs,
    )
    await session.commit()
    return issued.token


# ---------------------------------------------------------------------------------------------
# Invites (BND-T-23.2)
# ---------------------------------------------------------------------------------------------


async def test_the_manifest_names_both_lifecycle_endpoints(client: AsyncClient) -> None:
    endpoints = (await client.get("/.well-known/d3-app.json")).json()["endpoints"]
    assert endpoints["inviteAccept"] == "https://testserver/api/auth/native/invite"
    assert endpoints["deleteAccount"] == "https://testserver/api/auth/native/delete-account"


async def test_an_unknown_token_is_invite_invalid(client: AsyncClient) -> None:
    response = await client.post(
        INVITE,
        json={"token": "conformance-nope", "displayName": "X", "password": "a long enough one 1!",
              "device": DEVICE},
        headers=own_address(),
    )
    assert response.status_code == 410
    assert problem_type(response) == "invite_invalid"


async def test_an_invite_makes_an_account_enrols_and_signs_in(
    client: AsyncClient, session, user_factory
) -> None:
    admin, _ = await user_factory()
    token = await _invite(session, admin, storage_quota_bytes=5_000_000)
    address = own_address()

    first = await client.post(
        INVITE,
        json={"token": token, "displayName": "Guest", "password": "a sturdy passphrase here",
              "device": DEVICE},
        headers=address,
    )
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    enrolment = first.json()["enrolment"]
    assert enrolment["digits"] == 6 and enrolment["period"] == 30
    assert enrolment["otpauthUri"].startswith("otpauth://totp/")
    challenge = first.json()["challenge"]

    # A wrong code leaves the challenge usable.
    wrong = await client.post(
        INVITE, json={"challenge": challenge, "enrolTotp": "000000"}, headers=address
    )
    assert wrong.status_code == 401 and problem_type(wrong) == "invalid_code"

    code = _code_for_step(enrolment["secret"], current_step())
    second = await client.post(
        INVITE, json={"challenge": challenge, "enrolTotp": code}, headers=address
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["accessToken"] and body["refreshToken"] and body["session"]["id"]
    assert len(body["recoveryCodes"]) == 10

    me = await client.get("/api/auth/native/me", headers=bearer(body))
    assert me.status_code == 200 and me.json()["displayName"] == "Guest"

    # The invitation decided the library and the quota; the session is a native one.
    user = (
        await session.execute(sa.select(AppUser).where(AppUser.email == me.json()["email"]))
    ).scalar_one()
    await session.refresh(user)
    assert user.totp_enabled and user.storage_quota_bytes == 5_000_000
    library = (
        await session.execute(
            sa.select(Library).join(Membership).where(Membership.user_id == user.id)
        )
    ).scalar_one()
    assert library.name == "Guest papers" and library.kind == LibraryKind.PERSONAL
    row = await session.get(RefreshToken, uuid.UUID(body["session"]["id"]))
    assert row is not None and row.device_name == DEVICE["name"]
    created = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == user.id, AuditEvent.action == "account_created"
            )
        )
    ).scalar_one()
    assert created.after["via"] == "invitation" and created.after["client"] == "native"

    # The token is dead, and so is the challenge.
    again = await client.post(
        INVITE,
        json={"token": token, "displayName": "Again", "password": "a sturdy passphrase here",
              "device": DEVICE},
        headers=address,
    )
    assert again.status_code == 410 and problem_type(again) == "invite_invalid"
    replay = await client.post(
        INVITE,
        json={"challenge": challenge, "enrolTotp": _code_for_step(enrolment["secret"],
                                                                  current_step() + 1)},
        headers=address,
    )
    assert replay.status_code == 401


async def test_a_short_password_is_weak_password_with_words_to_act_on(
    client: AsyncClient, session, user_factory
) -> None:
    admin, _ = await user_factory()
    token = await _invite(session, admin)
    response = await client.post(
        INVITE,
        json={"token": token, "displayName": "G", "password": "short", "device": DEVICE},
        headers=own_address(),
    )
    assert response.status_code == 422 and problem_type(response) == "weak_password"
    assert response.json()["detail"] == "Use at least 12 characters."
    # The invitation is still good: a weak password is a typo, not a spent token.
    invitation = await accounts.find_invitation(session, token)
    assert invitation.accepted_at is None


async def test_a_withdrawn_or_expired_invite_is_invite_invalid(
    client: AsyncClient, session, user_factory
) -> None:
    admin, _ = await user_factory()
    withdrawn = await _invite(session, admin)
    expired = await _invite(session, admin)
    for token, change in ((withdrawn, "revoked_at"), (expired, "expires_at")):
        invitation = await accounts.find_invitation(session, token)
        setattr(invitation, change, datetime.now(UTC) - timedelta(minutes=1))
        await session.commit()
        response = await client.post(
            INVITE,
            json={"token": token, "displayName": "G", "password": "a sturdy passphrase here",
                  "device": DEVICE},
            headers=own_address(),
        )
        assert response.status_code == 410 and problem_type(response) == "invite_invalid"


async def test_an_admin_invite_link_is_an_invite_named_path(
    client: AsyncClient, session, signed_in
) -> None:
    admin, _ = await signed_in()
    await with_authenticator(session, admin)
    admin.is_admin = True
    await session.commit()
    response = await client.post(
        "/api/admin/invitations",
        json={"email": f"link-{uuid.uuid4().hex[:8]}@example.test", "library_name": "Docs"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["path"] == f"/invite/{response.json()['token']}"


# ---------------------------------------------------------------------------------------------
# Deletion (BND-T-23.3)
# ---------------------------------------------------------------------------------------------


async def _signed_in_natively(client, session, user_factory, **kwargs):
    user, library = await user_factory(**kwargs)
    secret = await with_authenticator(session, user)
    body = await sign_in(client, user, secret)
    return user, library, secret, body


def _next_code(secret: str) -> str:
    # Sign-in burned the current step; the next one is still inside the window.
    return _code_for_step(secret, current_step() + 1)


async def test_a_wrong_code_is_invalid_code_even_for_the_last_owner(
    client: AsyncClient, session, user_factory
) -> None:
    user, _, secret, body = await _signed_in_natively(client, session, user_factory)
    user.is_admin = True
    await session.commit()
    others = await _demote_other_admins(session, user)
    try:
        right = _next_code(secret)
        wrong = f"{(int(right) + 2) % 1_000_000:06d}"
        response = await client.post(
            DELETE, json={"confirmation": "testserver", "totp": wrong},
            headers={**bearer(body), **own_address()},
        )
        assert response.status_code == 401 and problem_type(response) == "invalid_code"
    finally:
        await _restore_admins(session, others)


async def _demote_other_admins(session, user: AppUser) -> list[uuid.UUID]:
    others = list(
        (
            await session.execute(
                sa.select(AppUser.id).where(AppUser.is_admin.is_(True), AppUser.id != user.id)
            )
        ).scalars()
    )
    await session.execute(sa.update(AppUser).where(AppUser.id.in_(others)).values(is_admin=False))
    await session.commit()
    return others


async def _restore_admins(session, others: list[uuid.UUID]) -> None:
    await session.execute(sa.update(AppUser).where(AppUser.id.in_(others)).values(is_admin=True))
    await session.commit()


async def test_the_only_administrator_is_last_owner(
    client: AsyncClient, session, user_factory
) -> None:
    user, _, secret, body = await _signed_in_natively(client, session, user_factory)
    user.is_admin = True
    await session.commit()
    others = await _demote_other_admins(session, user)
    try:
        response = await client.post(
            DELETE, json={"confirmation": "testserver", "totp": _next_code(secret)},
            headers={**bearer(body), **own_address()},
        )
        assert response.status_code == 409 and problem_type(response) == "last_owner"
        assert "administrator" in response.json()["detail"]
    finally:
        await _restore_admins(session, others)
    await session.refresh(user)
    assert user.is_active and user.delete_after is None


async def test_the_only_owner_of_a_library_others_use_is_last_owner(
    client: AsyncClient, session, user_factory
) -> None:
    _, library, secret, body = await _signed_in_natively(client, session, user_factory)
    library.name = "Household"
    await user_factory(library=library, role=MembershipRole.READER)
    await session.commit()
    response = await client.post(
        DELETE, json={"confirmation": "testserver", "totp": _next_code(secret)},
        headers={**bearer(body), **own_address()},
    )
    assert response.status_code == 409 and problem_type(response) == "last_owner"
    assert "Household" in response.json()["detail"]


async def test_a_mismatched_confirmation_is_422_naming_the_host(
    client: AsyncClient, session, user_factory
) -> None:
    _, _, secret, body = await _signed_in_natively(client, session, user_factory)
    response = await client.post(
        DELETE, json={"confirmation": "bindery.example", "totp": _next_code(secret)},
        headers={**bearer(body), **own_address()},
    )
    assert response.status_code == 422
    assert "testserver" in response.json()["detail"]


async def test_only_a_person_can_ask(
    client: AsyncClient, session, signed_in
) -> None:
    """An API token is not anybody, and a browser session is not the app."""
    user, library = await signed_in()
    await with_authenticator(session, user)
    issued = await tokens.issue(
        session, user_id=user.id, name="script", scopes=["admin"], library_ids=[library.id]
    )
    await session.commit()
    as_token = await client.post(
        DELETE, json={"confirmation": "testserver", "totp": "123456"},
        headers={"Authorization": f"Bearer {issued.secret}"},
    )
    assert as_token.status_code == 401 and problem_type(as_token) == "session_revoked"
    as_browser = await client.post(
        DELETE, json={"confirmation": "testserver", "totp": "123456"},
        headers={"Authorization": f"Bearer {client.cookies.get('bindery_access')}"},
    )
    assert as_browser.status_code == 401 and problem_type(as_browser) == "session_revoked"


async def test_deleting_schedules_a_week_out_and_ends_everything_at_once(
    client: AsyncClient, session, user_factory
) -> None:
    user, library, secret, body = await _signed_in_natively(client, session, user_factory)
    await service.issue_session(session, user)  # a browser session beside the phone's
    issued = await tokens.issue(
        session, user_id=user.id, name="script", scopes=["read"], library_ids=[library.id]
    )
    session.add(
        RelayRegistration(
            user_id=user.id, device_public_key=b"k" * 32, relay_url="https://relay.example",
            registration="r-1", send_key_sealed="sealed", categories=["bindery.registered"],
        )
    )
    await session.commit()

    response = await client.post(
        DELETE, json={"confirmation": "TestServer ", "totp": _next_code(secret)},
        headers={**bearer(body), **own_address()},
    )
    assert response.status_code == 202, response.text
    grace = datetime.fromisoformat(response.json()["graceUntil"].replace("Z", "+00:00"))
    assert grace >= datetime.now(UTC) + timedelta(days=6, hours=23)

    me = await client.get("/api/auth/native/me", headers=bearer(body))
    assert me.status_code == 401
    user_id, email, token_id = user.id, user.email, issued.record.id
    session.expire_all()
    live = await session.scalar(
        sa.select(sa.func.count()).select_from(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
    )
    assert live == 0, "a session — native or browser — outlived the request"
    token = await session.get(ApiToken, token_id)
    assert token is not None and token.revoked_at is not None
    relay = (
        await session.execute(
            sa.select(RelayRegistration).where(RelayRegistration.user_id == user_id)
        )
    ).scalar_one()
    assert relay.forgotten_at is not None
    await session.refresh(user)
    assert not user.is_active and user.delete_after == grace
    assert (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == user_id, AuditEvent.action == "deletion_requested"
            )
        )
    ).scalar_one()
    # Signing in again is refused, as for any disabled account.
    again = await client.post(
        "/api/auth/native/signin",
        json={"email": email, "password": PASSWORD, "device": DEVICE},
        headers=own_address(),
    )
    assert again.status_code == 401


async def _schedule(client, session, user_factory, **kwargs):
    user, library, secret, body = await _signed_in_natively(client, session, user_factory, **kwargs)
    response = await client.post(
        DELETE, json={"confirmation": "testserver", "totp": _next_code(secret)},
        headers={**bearer(body), **own_address()},
    )
    assert response.status_code == 202, response.text
    await session.refresh(user)
    return user, library


async def test_an_administrator_restoring_the_account_cancels_the_deletion(
    client: AsyncClient, session, user_factory
) -> None:
    user, _ = await _schedule(client, session, user_factory)
    user_id = user.id
    cancelled = await accounts.restore(session, user=user)
    await session.commit()
    await session.refresh(user)
    assert cancelled and user.is_active and user.delete_after is None

    report = await account_purge.purge_due(session, now=datetime.now(UTC) + timedelta(days=30))
    assert user_id not in [p.user_id for p in report.purged]
    await session.refresh(user)
    assert user.deleted_at is None


def _file(library_id: uuid.UUID, sha: str) -> SourceFile:
    return SourceFile(
        library_id=library_id, sha256=sha, byte_size=5, original_filename="deed.pdf",
        ingest_source=IngestSource.WEB_UPLOAD, page_count=1, state=SourceFileState.PROCESSED,
    )


async def _document(session, library_id: uuid.UUID, sha: str) -> Document:
    source = _file(library_id, sha)
    session.add(source)
    await session.flush()
    session.add(Page(source_file_id=source.id, page_number=1, text="a private page"))
    document = Document(
        library_id=library_id, source_file_id=source.id, page_start=1, page_end=1,
        title="Private deed", review_state=ReviewState.FILED,
    )
    session.add(document)
    await session.flush()
    return document


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _blob(sha: str):
    from api.storage.blobs import blob_path

    path = blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"bytes")
    return path


async def test_the_purge_removes_what_only_the_account_could_see_and_keeps_shared_documents(
    client: AsyncClient, session, user_factory, data_root
) -> None:
    from api.artifacts import derived_for
    from api.vault.store import new_object_name, object_path

    # The account's own library, with a document whose blob nobody else holds, a document whose
    # bytes another household's library also holds, and taxonomy.
    leaver, private = await user_factory()
    alone_sha, held_sha = uuid.uuid4().hex * 2, uuid.uuid4().hex * 2
    private_doc = await _document(session, private.id, alone_sha)
    held_doc = await _document(session, private.id, held_sha)
    session.add(Tag(library_id=private.id, name="Mine", slug=f"mine-{uuid.uuid4().hex[:6]}"))
    # A shared library somebody else owns, where the leaver added a document and vaulted another.
    owner, shared = await user_factory(library_name="Household")
    await user_factory(library=shared, role=MembershipRole.CONTRIBUTOR)
    session.add(
        Membership(user_id=leaver.id, library_id=shared.id, role=MembershipRole.CONTRIBUTOR)
    )
    kept_doc = await _document(session, shared.id, uuid.uuid4().hex * 2)
    vaulted_doc = await _document(session, shared.id, uuid.uuid4().hex * 2)
    vaulted_doc.vaulted_by = leaver.id
    vault = Vault(user_id=leaver.id, passphrase_wrapped={})
    session.add(vault)
    await session.flush()
    object_name = new_object_name()
    session.add(
        VaultItem(
            document_id=vaulted_doc.id, vault_id=vault.id, object_name=object_name, byte_size=1,
            sealed_sha256=b"x", sealed_meta=b"x", page_count=1,
        )
    )
    # Somebody else's library holding the same bytes as one of the leaver's documents.
    _, elsewhere = await user_factory()
    session.add(_file(elsewhere.id, held_sha))
    await session.flush()
    # Plain values from here on: a commit expires every loaded row.
    user_id, email, owner_id, vault_id = leaver.id, leaver.email, owner.id, vault.id
    private_id, shared_id = private.id, shared.id
    private_doc_id, held_doc_id = private_doc.id, held_doc.id
    vaulted_doc_id, kept_doc_id = vaulted_doc.id, kept_doc.id
    await session.commit()

    alone_blob, held_blob = _blob(alone_sha), _blob(held_sha)
    object_file = object_path(object_name)
    object_file.parent.mkdir(parents=True, exist_ok=True)
    object_file.write_bytes(b"ciphertext")
    classify = derived_for(held_sha).root / "classify"
    classify.mkdir(parents=True)
    (classify / f"{held_doc_id}-v1.request.json").write_text("{}")
    (classify / "someone-elses-v1.request.json").write_text("{}")

    user = await session.get(AppUser, user_id)
    assert user is not None
    secret = await with_authenticator(session, user)
    body = await sign_in(client, user, secret)
    scheduled = await client.post(
        DELETE, json={"confirmation": "testserver", "totp": _next_code(secret)},
        headers={**bearer(body), **own_address()},
    )
    assert scheduled.status_code == 202, scheduled.text

    # Inside the grace period nothing happens.
    early = await account_purge.purge_due(session, now=datetime.now(UTC) + timedelta(days=6))
    assert user_id not in [p.user_id for p in early.purged]
    assert await session.get(Document, private_doc_id) is not None

    # After it, on the test clock.
    report = await account_purge.purge_due(session, now=datetime.now(UTC) + timedelta(days=8))
    assert user_id in [p.user_id for p in report.purged]
    session.expire_all()

    assert await session.get(Library, private_id) is None
    for gone in (private_doc_id, held_doc_id, vaulted_doc_id):
        assert await session.get(Document, gone) is None
    assert not alone_blob.exists(), "a blob nobody holds any more was left on disk"
    assert held_blob.exists(), "another library's original was removed"
    assert not (classify / f"{held_doc_id}-v1.request.json").exists()
    assert (classify / "someone-elses-v1.request.json").exists()
    assert not object_file.exists()
    assert await session.scalar(
        sa.select(sa.func.count()).select_from(VaultItem).where(VaultItem.vault_id == vault_id)
    ) == 0

    # Shared: the library, its other members, and the document the leaver added.
    assert await session.get(Library, shared_id) is not None
    assert await session.get(Document, kept_doc_id) is not None
    assert await session.scalar(
        sa.select(sa.func.count()).select_from(Membership).where(Membership.user_id == user_id)
    ) == 0

    tombstone = await session.get(AppUser, user_id)
    assert tombstone is not None and tombstone.deleted_at is not None
    assert tombstone.email == account_purge.tombstone_email(user_id)
    assert tombstone.totp_secret is None and tombstone.display_name is None
    assert await session.scalar(
        sa.select(sa.func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)
    ) == 0
    purged = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == user_id, AuditEvent.action == "account_purged"
            )
        )
    ).scalar_one()
    assert purged.after["libraries_removed"] == 1 and purged.after["documents_removed"] == 3
    assert purged.after["shared_libraries_kept"] == 1

    # Idempotent, and the address can be invited again.
    again = await account_purge.purge_due(session, now=datetime.now(UTC) + timedelta(days=9))
    assert user_id not in [p.user_id for p in again.purged]
    owner_row = await session.get(AppUser, owner_id)
    assert owner_row is not None
    reinvited = await accounts.invite(
        session, email=email, library_name="Back again", created_by=owner_row
    )
    assert reinvited.token
    await session.rollback()


async def test_a_purged_account_cannot_be_restored(
    client: AsyncClient, session, user_factory, data_root
) -> None:
    user, _ = await _schedule(client, session, user_factory)
    user_id = user.id
    await account_purge.purge_due(session, now=datetime.now(UTC) + timedelta(days=8))
    session.expire_all()
    tombstone = await session.get(AppUser, user_id)
    assert tombstone is not None
    with pytest.raises(accounts.AccountError):
        await accounts.restore(session, user=tombstone)
    listed = (
        await session.execute(sa.select(Invitation).where(Invitation.accepted_user_id == user_id))
    ).scalars().all()
    assert all(row.email.endswith("@deleted.invalid") for row in listed)
