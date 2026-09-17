"""Sign in with D3 Auth: the rules, without the provider (Phase 20).

Everything here is Bindery's half — the transaction cookie, who gets provisioned, what a role
maps to, and which sessions a logout ends. The flow against a real provider is exercised in the
e2e suite; these are the decisions that have to hold whatever the provider says.
"""

from __future__ import annotations

import time
import uuid

import pytest
import sqlalchemy as sa

from api import oidc, settings_store
from api.auth import service
from api.db.models import AppUser, Membership, OidcIdentity
from api.db.models.user import RefreshToken

GOOD_PASSWORD = "quiet harbour lantern orchard"
ISSUER = "https://auth.example.test"


def a_subject() -> str:
    """A provider subject nobody else in this suite is using.

    The test database is shared across the module and `(issuer, subject)` is unique, so a fixed
    string would make the second test to run fail on the first one's row.
    """
    return f"person-{uuid.uuid4().hex[:12]}"


async def configure(session, *, mode: str = "optional") -> None:
    for key, value in (
        (settings_store.OIDC_ISSUER, "https://auth.example.test"),
        (settings_store.OIDC_CLIENT_ID, "bindery"),
        (settings_store.OIDC_CLIENT_SECRET, "a-client-secret"),
        (settings_store.SSO_MODE, mode),
    ):
        await settings_store.set_(session, key, value, actor_id=None)
    await session.commit()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


async def unconfigure(session) -> None:
    """Back to a fresh archive: settings are a shared table, and other tests write it."""
    for key in (
        settings_store.OIDC_ISSUER,
        settings_store.OIDC_CLIENT_ID,
        settings_store.OIDC_CLIENT_SECRET,
        settings_store.SSO_MODE,
    ):
        await settings_store.set_(session, key, None, actor_id=None)
    await session.commit()


async def test_sso_is_off_until_an_operator_configures_it(session) -> None:
    """A self-hosted archive should not mention a provider nobody has set up."""
    await unconfigure(session)
    configuration = await oidc.config(session)
    assert configuration.mode == "off"
    assert configuration.enabled is False


async def test_a_half_configured_provider_is_not_enabled(session) -> None:
    await unconfigure(session)
    await settings_store.set_(session, settings_store.SSO_MODE, "optional", actor_id=None)
    await settings_store.set_(session, settings_store.OIDC_ISSUER, "https://auth.example.test", actor_id=None)
    await session.commit()
    assert (await oidc.config(session)).enabled is False, "no client id and no secret is not configured"


async def test_the_client_secret_is_never_read_back(client, signed_in, session) -> None:
    """REQ-203 — the same posture as the AI key: writable, never returned."""
    user, _ = await signed_in()
    user.is_admin = True
    await session.commit()
    await configure(session)

    body = (await client.get("/api/settings")).json()
    assert "a-client-secret" not in str(body)


# ---------------------------------------------------------------------------
# The transaction cookie (finding F-12)
# ---------------------------------------------------------------------------


def test_a_transaction_survives_a_round_trip() -> None:
    sealed = oidc.seal_transaction({"verifier": "v", "state": "s", "nonce": "n", "link": None})
    opened = oidc.open_transaction(sealed)
    assert (opened["verifier"], opened["state"], opened["nonce"]) == ("v", "s", "n")


def test_a_transaction_nobody_here_sealed_is_refused() -> None:
    """The cookie is signed, so a forged one is not a sign-in somebody else can begin."""
    sealed = oidc.seal_transaction({"verifier": "v", "state": "s", "nonce": "n"})
    body, _, signature = sealed.partition(".")
    with pytest.raises(oidc.SignInRefused):
        oidc.open_transaction(f"{body}.{'0' * len(signature)}")
    with pytest.raises(oidc.SignInRefused):
        oidc.open_transaction("not-a-transaction")
    with pytest.raises(oidc.SignInRefused):
        oidc.open_transaction(None)


def test_a_transaction_left_open_too_long_is_refused(monkeypatch) -> None:
    sealed = oidc.seal_transaction({"verifier": "v", "state": "s", "nonce": "n"})
    later = time.time() + oidc.TRANSACTION_TTL_SECONDS + 5
    monkeypatch.setattr(oidc.time, "time", lambda: later)
    with pytest.raises(oidc.SignInRefused, match="too long"):
        oidc.open_transaction(sealed)


def test_the_verifier_is_not_readable_by_whoever_holds_the_state() -> None:
    """The point of F-12: the transaction belongs to a browser, not to a state value.

    Nothing here is keyed on `state`, so there is no lookup for an attacker to make. The only
    copy of the verifier is in the cookie the browser that started it holds.
    """
    first = oidc.open_transaction(oidc.seal_transaction({"verifier": "mine", "state": "shared", "nonce": "n"}))
    second = oidc.open_transaction(oidc.seal_transaction({"verifier": "theirs", "state": "shared", "nonce": "n"}))
    assert first["verifier"] != second["verifier"]


# ---------------------------------------------------------------------------
# Provisioning (REQ-205)
# ---------------------------------------------------------------------------


async def test_a_first_sign_in_with_a_role_gets_an_account_a_library_and_a_quota(session) -> None:
    user = await oidc.provision(
        session,
        issuer=ISSUER,
        subject=a_subject(),
        email=f"Guest-{uuid.uuid4().hex[:6]}@Example.test",
        display_name="A Guest",
        preferred_username="guest",
        roles=["guest"],
        refresh_token="a-refresh-token",
    )
    await session.commit()

    assert user.email == user.email.lower(), "an address is stored as it is compared"
    assert user.is_admin is False
    assert user.storage_quota_bytes == oidc.GUEST_QUOTA_BYTES
    memberships = (
        await session.execute(sa.select(Membership).where(Membership.user_id == user.id))
    ).scalars().all()
    assert len(memberships) == 1, "one account, one personal library, owner of nothing else"

    identity = await oidc.identity_of(session, user=user)
    assert identity is not None and identity.origin == "sso"
    assert identity.refresh_token_enc and "a-refresh-token" not in identity.refresh_token_enc
    assert oidc.decrypt_token(identity.refresh_token_enc) == "a-refresh-token"


async def test_an_identity_with_no_role_provisions_nothing(session) -> None:
    """D3 Auth is deny-by-default, so no role means no decision was made about this person."""
    with pytest.raises(oidc.SignInRefused, match="no access"):
        await oidc.provision(
            session,
            issuer=ISSUER,
            subject=a_subject(),
            email="stranger@example.test",
            display_name=None,
            preferred_username=None,
            roles=[],
            refresh_token=None,
        )
    assert (
        await session.execute(sa.select(AppUser).where(AppUser.email == "stranger@example.test"))
    ).scalar_one_or_none() is None


async def test_an_address_that_already_exists_here_is_never_taken_over(session, user_factory) -> None:
    """The case auto-linking by email gets wrong, refused in words that say what to do."""
    existing, _ = await user_factory("shared@example.test")
    await session.commit()

    with pytest.raises(oidc.SignInRefused, match="connect D3 Auth from Settings"):
        await oidc.provision(
            session,
            issuer=ISSUER,
            subject=a_subject(),
            email="shared@example.test",
            display_name=None,
            preferred_username=None,
            roles=["member"],
            refresh_token=None,
        )
    assert await oidc.identity_of(session, user=existing) is None


async def test_a_member_is_not_capped_and_a_guest_is() -> None:
    assert oidc.quota_for(["member"]) is None
    assert oidc.quota_for(["admin"]) is None
    assert oidc.quota_for(["guest"]) == oidc.GUEST_QUOTA_BYTES


# ---------------------------------------------------------------------------
# Linking (REQ-206)
# ---------------------------------------------------------------------------


async def test_an_identity_belongs_to_one_account_and_an_account_to_one_identity(session, user_factory) -> None:
    first, _ = await user_factory()
    second, _ = await user_factory()
    subject = a_subject()
    await oidc.link(
        session, user=first, issuer=ISSUER, subject=subject,
        preferred_username="one", refresh_token=None, origin="link",
    )
    await session.commit()

    with pytest.raises(oidc.SignInRefused, match="already connected to somebody"):
        await oidc.link(
            session, user=second, issuer=ISSUER, subject=subject,
            preferred_username="one", refresh_token=None, origin="link",
        )
    with pytest.raises(oidc.SignInRefused, match="already connected"):
        await oidc.link(
            session, user=first, issuer=ISSUER, subject=a_subject(),
            preferred_username="two", refresh_token=None, origin="link",
        )


async def test_disconnecting_leaves_the_account_and_its_documents_alone(session, user_factory) -> None:
    user, _ = await user_factory()
    await oidc.link(
        session, user=user, issuer=ISSUER, subject=a_subject(),
        preferred_username=None, refresh_token="r", origin="link",
    )
    await session.commit()

    assert await oidc.disconnect(session, user=user) is True
    await session.commit()
    assert await oidc.identity_of(session, user=user) is None
    assert await session.get(AppUser, user.id) is not None
    assert await oidc.disconnect(session, user=user) is False, "disconnecting twice is not an error"


# ---------------------------------------------------------------------------
# Roles (REQ-207) — and the rule that does not move
# ---------------------------------------------------------------------------


async def test_the_admin_role_does_not_make_an_administrator_without_bindery_two_factor(session, user_factory) -> None:
    """REQ-156 is Bindery's rule, and an identity arriving from elsewhere does not lift it.

    An administrator here can issue a reset code for every other account. That is why two-factor
    is mandatory, and why a claim made at another server cannot be the thing that grants it.
    """
    user, _ = await user_factory()
    assert user.totp_enabled is False

    changed = await oidc.apply_roles(session, user=user, roles=["admin"])
    assert changed is False
    assert user.is_admin is False


async def test_the_admin_role_applies_once_two_factor_is_enrolled(session, user_factory) -> None:
    from api.auth import totp

    user, _ = await user_factory()
    user.totp_secret = totp.new_secret()
    user.totp_confirmed_at = sa.func.now()
    await session.flush()
    await session.refresh(user)

    assert await oidc.apply_roles(session, user=user, roles=["admin"]) is True
    assert user.is_admin is True


async def test_losing_the_admin_role_takes_it_away_here(session, user_factory) -> None:
    from api.auth import totp

    user, _ = await user_factory()
    user.totp_secret = totp.new_secret()
    user.totp_confirmed_at = sa.func.now()
    await session.flush()
    await session.refresh(user)
    await oidc.apply_roles(session, user=user, roles=["admin"])

    assert await oidc.apply_roles(session, user=user, roles=["member"]) is True
    assert user.is_admin is False


# ---------------------------------------------------------------------------
# Back-channel logout (REQ-208)
# ---------------------------------------------------------------------------


async def test_a_logout_naming_a_session_ends_that_one_only(session, user_factory) -> None:
    """Signing out on the phone must not sign the laptop out too."""
    user, _ = await user_factory()
    subject = a_subject()
    await oidc.link(
        session, user=user, issuer=ISSUER, subject=subject,
        preferred_username=None, refresh_token=None, origin="sso",
    )

    await service.issue_session(session, user)
    phone = (
        await session.execute(
            sa.select(RefreshToken).where(RefreshToken.user_id == user.id).order_by(RefreshToken.issued_at.desc())
        )
    ).scalars().first()
    phone.oidc_sid = "phone-session"
    await service.issue_session(session, user)
    await session.commit()

    ended = await oidc.end_sessions(session, issuer=ISSUER, subject=subject, sid="phone-session")
    await session.commit()

    assert ended == 1
    live = (
        await session.execute(
            sa.select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        )
    ).scalars().all()
    assert len(live) == 1 and live[0].oidc_sid is None, "the laptop is still signed in"


async def test_a_logout_naming_no_session_ends_every_session_this_identity_holds(session, user_factory) -> None:
    user, _ = await user_factory()
    subject = a_subject()
    await oidc.link(
        session, user=user, issuer=ISSUER, subject=subject,
        preferred_username=None, refresh_token=None, origin="sso",
    )
    for _ in range(3):
        await service.issue_session(session, user)
    await session.commit()

    ended = await oidc.end_sessions(session, issuer=ISSUER, subject=subject, sid=None)
    await session.commit()
    assert ended == 3


async def test_a_logout_for_an_identity_nobody_here_has_ends_nothing(session) -> None:
    assert await oidc.end_sessions(session, issuer=ISSUER, subject=a_subject(), sid=None) == 0
