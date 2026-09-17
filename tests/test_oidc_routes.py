"""The routes of Sign in with D3 Auth, against a provider that does as it is told (Phase 20).

The SDK is stood in for, because what is under test is Bindery's half: which callback is
accepted, which account it lands on, what a link does that a sign-in does not, and which
sessions a logout ends. The real provider is exercised by the e2e suite.
"""

from __future__ import annotations

import sys
import types
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from api import oidc, settings_store
from api.auth import service
from api.db.models import AppUser, OidcIdentity, OidcLogoutEvent
from api.db.models.user import RefreshToken
from tests.conftest import PASSWORD
from tests.test_oidc import ISSUER, a_subject

CONFIGURED = {"issuer": ISSUER, "client_id": "bindery", "secret": "a-client-secret"}


# ---------------------------------------------------------------------------
# A provider that answers exactly what a test tells it to
# ---------------------------------------------------------------------------


@dataclass
class FakeIdentity:
    iss: str
    sub: str
    claims: dict[str, Any]
    roles: list[str]


@dataclass
class FakeSession:
    identity: FakeIdentity
    access_token: str = "an-access-token"
    id_token: str = "an-id-token"
    refresh_token: str | None = "a-refresh-token"
    expires_at: int | None = None
    sid: str | None = "a-provider-session"


class FakeProvider:
    """What the SDK would have returned, and a record of what it was asked."""

    def __init__(self) -> None:
        self.healthy_answer = True
        self.session: FakeSession | None = None
        self.refuse_with: Exception | None = None
        self.logout: Any | None = None
        self.logout_refusal: Exception | None = None
        self.starts = 0

    def sign_in_as(
        self,
        sub: str,
        *,
        roles: list[str],
        email: str,
        sid: str | None = "a-provider-session",
        name: str | None = None,
        username: str | None = None,
    ) -> None:
        claims = {
            "sub": sub, "email": email, "name": name,
            "preferred_username": username, "roles": roles,
        }
        self.session = FakeSession(
            identity=FakeIdentity(iss=ISSUER, sub=sub, claims=claims, roles=roles), sid=sid
        )


@pytest.fixture(autouse=True)
def own_address(client):
    """Each test signs in from its own address.

    The callback spends the same per-address budget as the password form — a provider is not a
    way around the rate limit — so tests sharing one address would spend each other's, and the
    ones that run later would see 429s that have nothing to do with them.
    """
    client.headers["cf-connecting-ip"] = f"198.51.100.{uuid.uuid4().int % 250 + 1}"
    yield
    client.headers.pop("cf-connecting-ip", None)


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    """Installs a stand-in `d3auth_client`, since the real one is imported where it is used."""
    fake = FakeProvider()

    @dataclass
    class SignInStart:
        url: str
        verifier: str
        state: str
        nonce: str

    class D3AuthClient:
        def __init__(self, **_: Any) -> None:
            pass

        def healthy(self) -> bool:
            return fake.healthy_answer

        async def start_sign_in(self, redirect_uri: str, **_: Any) -> SignInStart:
            fake.starts += 1
            return SignInStart(
                url=f"{ISSUER}/oidc/auth?state=a-state",
                verifier="a-verifier",
                state="a-state",
                nonce="a-nonce",
            )

        async def finish_sign_in(
            self, callback_url: str, *, start: Any, redirect_uri: str
        ) -> FakeSession:
            if fake.refuse_with is not None:
                raise fake.refuse_with
            # The real SDK refuses a callback whose state is not the one this browser began,
            # compared in full — a state that merely starts the same is a different state.
            from urllib.parse import parse_qs, urlparse

            returned = (parse_qs(urlparse(str(callback_url)).query).get("state") or [""])[0]
            if returned != start.state:
                raise ValueError("this callback belongs to a different sign-in")
            assert fake.session is not None, "the test did not say who is signing in"
            return fake.session

    def verify_logout_token(token: str, **_: Any) -> Any:
        if fake.logout_refusal is not None:
            raise fake.logout_refusal
        return fake.logout

    module = types.ModuleType("d3auth_client")
    module.D3AuthClient = D3AuthClient  # type: ignore[attr-defined]
    module.SignInStart = SignInStart  # type: ignore[attr-defined]
    module.verify_logout_token = verify_logout_token  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "d3auth_client", module)
    return fake


async def configure(session, *, mode: str = "optional") -> None:
    for key, value in (
        (settings_store.OIDC_ISSUER, CONFIGURED["issuer"]),
        (settings_store.OIDC_CLIENT_ID, CONFIGURED["client_id"]),
        (settings_store.OIDC_CLIENT_SECRET, CONFIGURED["secret"]),
        (settings_store.SSO_MODE, mode),
    ):
        await settings_store.set_(session, key, value, actor_id=None)
    await session.commit()


def callback_url(state: str = "a-state") -> str:
    return f"/api/auth/oidc/callback?code=an-authorization-code&state={state}&iss={ISSUER}"


async def begin(client) -> None:
    """Start a sign-in, so the browser holds the transaction its callback will need."""
    response = await client.get("/api/auth/oidc/start")
    assert response.status_code == 302, response.text


# ---------------------------------------------------------------------------
# Off is off (REQ-209)
# ---------------------------------------------------------------------------


async def test_an_archive_that_never_configured_sso_says_nothing_about_a_provider(
    client, session
) -> None:
    await settings_store.set_(session, settings_store.SSO_MODE, "off", actor_id=None)
    await session.commit()

    body = (await client.get("/api/auth/oidc/status")).json()
    assert body == {"mode": "off", "ready": False, "issuer": None}


async def test_every_sso_route_is_absent_while_sso_is_off(client, session, provider) -> None:
    await settings_store.set_(session, settings_store.SSO_MODE, "off", actor_id=None)
    await session.commit()

    assert (await client.get("/api/auth/oidc/start")).status_code == 404
    assert (await client.get(callback_url())).status_code == 404
    logout = await client.post("/api/auth/oidc/backchannel-logout", data={"logout_token": "x"})
    assert logout.status_code == 404


async def test_the_status_says_when_the_provider_cannot_be_reached(
    client, session, provider
) -> None:
    await configure(session, mode="required")
    provider.healthy_answer = False

    body = (await client.get("/api/auth/oidc/status")).json()
    assert body["mode"] == "required" and body["ready"] is False and body["issuer"] == ISSUER


# ---------------------------------------------------------------------------
# Signing in (REQ-204)
# ---------------------------------------------------------------------------


async def test_a_sign_in_begins_with_a_transaction_bound_to_this_browser(
    client, session, provider
) -> None:
    await configure(session)
    response = await client.get("/api/auth/oidc/start")

    assert response.status_code == 302
    assert response.headers["location"].startswith(f"{ISSUER}/oidc/auth")
    cookie = response.cookies.get("bindery_oidc_tx")
    assert cookie, "the verifier, state and nonce travel with the browser, not in a table"
    opened = oidc.open_transaction(cookie)
    assert opened["state"] == "a-state"
    assert opened["verifier"] == "a-verifier"
    assert opened["link"] is None


async def test_a_callback_without_a_transaction_signs_nobody_in(client, session, provider) -> None:
    await configure(session)
    provider.sign_in_as(a_subject(), roles=["member"], email="somebody@example.test")

    response = await client.get(callback_url())

    assert response.status_code == 303
    assert "sso=expired" in response.headers["location"]
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_a_callback_from_somebody_elses_sign_in_is_refused(client, session, provider) -> None:
    await configure(session)
    provider.sign_in_as(a_subject(), roles=["member"], email="somebody@example.test")
    await begin(client)

    response = await client.get(callback_url(state="a-state-from-another-browser"))

    assert response.status_code == 303 and "sso=refused" in response.headers["location"]
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_a_first_sign_in_with_a_role_lands_in_a_new_account(
    client, session, provider
) -> None:
    await configure(session)
    subject, email = a_subject(), f"new-{uuid.uuid4().hex[:6]}@example.test"
    provider.sign_in_as(subject, roles=["member"], email=email, name="A Newcomer")
    await begin(client)

    response = await client.get(callback_url())
    assert response.status_code == 303 and response.headers["location"] == "/"

    me = await client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["email"] == email

    identity = (
        await session.execute(sa.select(OidcIdentity).where(OidcIdentity.subject == subject))
    ).scalar_one()
    assert identity.origin == "sso"


async def test_a_sign_in_with_no_role_creates_nothing(client, session, provider) -> None:
    await configure(session)
    email = f"nobody-{uuid.uuid4().hex[:6]}@example.test"
    provider.sign_in_as(a_subject(), roles=[], email=email)
    await begin(client)

    response = await client.get(callback_url())

    assert response.status_code == 303 and "sso=refused" in response.headers["location"]
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (
        await session.execute(sa.select(AppUser).where(AppUser.email == email))
    ).scalar_one_or_none() is None


async def test_a_returning_identity_lands_in_the_account_it_is_linked_to(
    client, session, user_factory, provider
) -> None:
    await configure(session)
    user, _ = await user_factory()
    subject = a_subject()
    await oidc.link(
        session, user=user, issuer=ISSUER, subject=subject,
        preferred_username=None, refresh_token=None, origin="link",
    )
    await session.commit()

    provider.sign_in_as(subject, roles=["member"], email="whatever-the-provider-says@example.test")
    await begin(client)
    response = await client.get(callback_url())

    assert response.status_code == 303
    assert (await client.get("/api/auth/me")).json()["email"] == user.email, (
        "the link decides whose account this is, not the address the provider sent"
    )


async def test_the_provider_session_rides_on_the_bindery_session_it_produced(
    client, session, provider
) -> None:
    await configure(session)
    subject = a_subject()
    provider.sign_in_as(subject, roles=["member"], email=f"sid-{uuid.uuid4().hex[:6]}@example.test",
                        sid="the-provider-session")
    await begin(client)
    await client.get(callback_url())

    identity = (
        await session.execute(sa.select(OidcIdentity).where(OidcIdentity.subject == subject))
    ).scalar_one()
    tokens = (
        await session.execute(
            sa.select(RefreshToken).where(RefreshToken.user_id == identity.user_id)
        )
    ).scalars().all()
    assert [token.oidc_sid for token in tokens] == ["the-provider-session"]


# ---------------------------------------------------------------------------
# Linking (REQ-206)
# ---------------------------------------------------------------------------


async def test_linking_anonymously_is_not_a_thing(client, session, provider) -> None:
    await configure(session)
    assert (await client.get("/api/auth/oidc/link/start")).status_code == 401


async def test_a_link_attaches_the_identity_without_changing_who_is_signed_in(
    client, session, signed_in, provider
) -> None:
    await configure(session)
    user, _ = await signed_in()
    subject = a_subject()
    provider.sign_in_as(
        subject, roles=["member"], email="a-different-address@example.test", username="them"
    )

    start = await client.get("/api/auth/oidc/link/start")
    assert start.status_code == 302
    assert oidc.open_transaction(start.cookies.get("bindery_oidc_tx"))["link"] == str(user.id)

    response = await client.get(callback_url())
    assert response.status_code == 303 and "linked=1" in response.headers["location"]

    card = (await client.get("/api/auth/oidc/link")).json()
    assert card["linked"] is True and card["preferred_username"] == "them"
    assert (await client.get("/api/auth/me")).json()["email"] == user.email


async def test_disconnecting_needs_the_local_password(client, session, signed_in, provider) -> None:
    await configure(session)
    user, _ = await signed_in()
    await oidc.link(
        session, user=user, issuer=ISSUER, subject=a_subject(),
        preferred_username=None, refresh_token=None, origin="link",
    )
    await session.commit()

    refused = await client.post("/api/auth/oidc/disconnect", data={"password": "not the password"})
    assert refused.status_code == 403
    assert (await client.get("/api/auth/oidc/link")).json()["linked"] is True

    accepted = await client.post("/api/auth/oidc/disconnect", data={"password": PASSWORD})
    assert accepted.status_code == 204
    assert (await client.get("/api/auth/oidc/link")).json()["linked"] is False


# ---------------------------------------------------------------------------
# Back-channel logout (REQ-208)
# ---------------------------------------------------------------------------


@dataclass
class Logout:
    sub: str
    jti: str
    sid: str | None


async def test_a_logout_token_ends_the_session_it_names_and_only_once(
    client, session, user_factory, provider
) -> None:
    await configure(session)
    user, _ = await user_factory()
    subject = a_subject()
    await oidc.link(
        session, user=user, issuer=ISSUER, subject=subject,
        preferred_username=None, refresh_token=None, origin="sso",
    )
    await service.issue_session(session, user)
    token = (
        await session.execute(
            sa.select(RefreshToken)
            .where(RefreshToken.user_id == user.id)
            .order_by(RefreshToken.issued_at.desc())
        )
    ).scalars().first()
    token.oidc_sid = "a-provider-session"
    await session.commit()

    provider.logout = Logout(sub=subject, jti="an-event", sid="a-provider-session")
    first = await client.post("/api/auth/oidc/backchannel-logout", data={"logout_token": "a-token"})
    assert first.status_code == 200

    await session.refresh(token)
    assert token.revoked_at is not None

    # A retry must not end a session the person has since started again.
    await service.issue_session(session, user)
    await session.commit()
    repeat = await client.post(
        "/api/auth/oidc/backchannel-logout", data={"logout_token": "a-token"}
    )
    assert repeat.status_code == 200, "a repeat is a 200, or the provider retries for nothing"

    live = (
        await session.execute(
            sa.select(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    assert len(live) == 1, "the newer session survived the repeated event"

    events = (
        await session.execute(sa.select(OidcLogoutEvent).where(OidcLogoutEvent.jti == "an-event"))
    ).scalars().all()
    assert len(events) == 1


async def test_a_logout_token_that_does_not_verify_is_a_bad_request(
    client, session, provider
) -> None:
    await configure(session)
    provider.logout_refusal = ValueError("logout token rejected: bad signature")

    response = await client.post(
        "/api/auth/oidc/backchannel-logout", data={"logout_token": "forged"}
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Roles re-read when the session renews (REQ-207)
# ---------------------------------------------------------------------------


@dataclass
class RenewedSession:
    """The SDK's `Session`, as `refresh()` answers it: the roles hang off the identity.

    Deliberately not a `roles` attribute of its own. The first version of this fake had one,
    which agreed with a bug in `refresh_roles` and hid it until the SDK began shipping types.
    """

    identity: FakeIdentity
    access_token: str = "a-renewed-access-token"
    refresh_token: str | None = "a-renewed-refresh-token"


async def test_a_role_withdrawn_at_the_provider_lands_on_the_next_renewal(
    client, session, user_factory, provider, monkeypatch
) -> None:
    """A grant taken away upstream must not wait for a sign-in that may never come again."""
    from api.auth import totp

    user, _ = await user_factory()
    user.totp_secret = totp.new_secret()
    user.totp_confirmed_at = sa.func.now()
    await session.flush()
    await session.refresh(user)
    await oidc.link(
        session, user=user, issuer=ISSUER, subject=a_subject(),
        preferred_username=None, refresh_token="a-provider-refresh-token", origin="link",
    )
    await oidc.apply_roles(session, user=user, roles=["admin"])
    await session.commit()
    assert user.is_admin is True
    await configure(session)

    renewed = RenewedSession(
        identity=FakeIdentity(iss=ISSUER, sub="x", claims={}, roles=["member"]),
        refresh_token="a-rotated-provider-token",
    )

    class Renewing:
        def __init__(self, **_: Any) -> None:
            pass

        async def refresh(self, _token: str) -> RenewedSession:
            return renewed

    monkeypatch.setattr(sys.modules["d3auth_client"], "D3AuthClient", Renewing)

    assert await oidc.refresh_roles(session, user=user) == ["member"]
    await session.commit()
    await session.refresh(user)

    assert user.is_admin is False, "the admin screens are gone on the next request"
    identity = await oidc.identity_of(session, user=user)
    assert oidc.decrypt_token(identity.refresh_token_enc) == "a-rotated-provider-token", (
        "a rotated provider token replaces the one it rotated"
    )


async def test_a_provider_that_cannot_be_reached_does_not_sign_anybody_out(
    client, session, user_factory, provider, monkeypatch
) -> None:
    user, _ = await user_factory()
    await oidc.link(
        session, user=user, issuer=ISSUER, subject=a_subject(),
        preferred_username=None, refresh_token="a-provider-refresh-token", origin="link",
    )
    await session.commit()
    await configure(session)

    class Unreachable:
        def __init__(self, **_: Any) -> None:
            pass

        async def refresh(self, _token: str) -> Any:
            raise OSError("the provider is not answering")

    monkeypatch.setattr(sys.modules["d3auth_client"], "D3AuthClient", Unreachable)

    assert await oidc.refresh_roles(session, user=user) is None
    assert await oidc.identity_of(session, user=user) is not None, "the link survives an outage"


async def test_an_account_with_no_link_asks_the_provider_nothing(
    session, user_factory, provider
) -> None:
    user, _ = await user_factory()
    assert await oidc.refresh_roles(session, user=user) is None


async def test_renewing_the_session_is_what_re_reads_the_roles(
    client, session, signed_in, provider, monkeypatch
) -> None:
    """Through the route, because the service being right is not the same as it being called."""
    from api.auth import totp

    user, _ = await signed_in()
    user.totp_secret = totp.new_secret()
    # A real timestamp, not sa.func.now(): the property reads it back before the flush has been
    # refreshed, and a SQL expression there is a lazy load in the wrong place.
    user.totp_confirmed_at = datetime.now(UTC)
    await session.flush()
    await oidc.link(
        session, user=user, issuer=ISSUER, subject=a_subject(),
        preferred_username=None, refresh_token="a-provider-refresh-token", origin="link",
    )
    await oidc.apply_roles(session, user=user, roles=["admin"])
    await session.commit()
    await configure(session)

    demoted = RenewedSession(
        identity=FakeIdentity(iss=ISSUER, sub=a_subject(), claims={}, roles=["member"]),
        refresh_token=None,
    )

    class Demoting:
        def __init__(self, **_: Any) -> None:
            pass

        async def refresh(self, _token: str) -> RenewedSession:
            return demoted

    monkeypatch.setattr(sys.modules["d3auth_client"], "D3AuthClient", Demoting)

    assert (await client.get("/api/auth/me")).json()["is_admin"] is True
    assert (await client.post("/api/auth/refresh")).status_code == 200
    assert (await client.get("/api/auth/me")).json()["is_admin"] is False
