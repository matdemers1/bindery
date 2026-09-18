"""Configuring Sign in with D3 Auth from the settings screen (Phase 20, REQ-203).

The phase shipped with the relying-party half built, deployed, and reachable only by editing a
compose file over SSH: `settings_store` accepted the keys and nothing in the UI ever wrote them.
This is the panel that was missing, and these are the rules the panel cannot be trusted to keep
on its own.

Two of them matter more than the form does. **Which provider may mint sessions here is the
administrator's** — an account that could point this at a provider it controls could grant
itself `admin` there and arrive holding it, which is the whole archive through a settings form.
And **SSO cannot be turned on half-configured**: a mode of `optional` with no issuer puts a
button on the front door that cannot work, and in `required` mode that button *is* the front
door.
"""

import pytest
import sqlalchemy as sa

from api import settings_store
from api.db.models import Setting
from tests.conftest import PASSWORD

ISSUER = "https://auth.example.test"
SECRET = "a-client-secret-from-the-connection-sheet"


async def _save(client, **fields):
    return await client.put("/api/settings", json=fields)


@pytest.fixture(autouse=True)
async def unconfigured(session):
    """Settings are one row per key for the whole archive, so they outlive a test.

    Without this, a test asserting that SSO cannot be turned on without a provider passes or
    fails depending on whether an earlier test in the file happened to configure one.
    """
    await session.execute(
        sa.delete(Setting).where(
            Setting.key.in_(
                [
                    settings_store.OIDC_ISSUER,
                    settings_store.OIDC_CLIENT_ID,
                    settings_store.OIDC_CLIENT_SECRET,
                    settings_store.SSO_MODE,
                ]
            )
        )
    )
    await session.commit()


@pytest.fixture
async def admin(session, signed_in):
    user, _ = await signed_in()
    user.is_admin = True
    await session.commit()
    return user


@pytest.fixture
async def member(session, signed_in):
    """An ordinary account. It owns its own library, which is the point of the test below."""
    user, _ = await signed_in()
    user.is_admin = False
    await session.commit()
    return user


async def test_the_client_secret_never_comes_back(client, admin):
    assert (
        await _save(client, oidc_issuer=ISSUER, oidc_client_id="bindery", oidc_client_secret=SECRET)
    ).status_code == 200

    answer = await client.get("/api/settings")
    body = answer.json()
    assert body["oidc_secret_configured"] is True
    assert body["oidc_secret_hint"] == "…heet"
    assert SECRET not in answer.text


async def test_the_issuer_and_client_id_do_come_back_in_full(client, admin):
    """Deliberate, and the opposite of the rule above.

    Both are printed on the provider's connection sheet and appear in every authorization URL.
    You cannot check that a redirect URI matches an issuer you are not allowed to read.
    """
    await _save(client, oidc_issuer=ISSUER, oidc_client_id="bindery")

    body = (await client.get("/api/settings")).json()
    assert body["oidc_issuer"] == ISSUER
    assert body["oidc_client_id"] == "bindery"


async def test_a_household_member_cannot_choose_the_provider(client, member):
    """`_owner_only` is satisfied by owning any library, and every invited account owns one."""
    answer = await _save(client, oidc_issuer="https://auth.attacker.test")
    assert answer.status_code == 403
    assert "administrator" in answer.json()["detail"]


async def test_the_provider_is_not_readable_by_everybody(client, session, member, admin):
    """Which provider holds the keys to the archive is a statement about the host."""
    await _save(client, oidc_issuer=ISSUER, oidc_client_id="bindery", oidc_client_secret=SECRET)

    # Now ask as the ordinary account: the same `None` a non-configured archive returns, which
    # says nothing at all rather than "not for you".
    await client.post("/api/auth/login", json={"email": member.email, "password": PASSWORD})
    body = (await client.get("/api/settings")).json()
    assert body["oidc_issuer"] is None
    assert body["oidc_client_id"] is None
    assert body["oidc_secret_configured"] is False


@pytest.mark.parametrize("mode", ["optional", "required"])
async def test_sso_cannot_be_turned_on_without_a_provider(client, admin, mode) -> None:
    answer = await _save(client, sso_mode=mode)
    assert answer.status_code == 422
    detail = answer.json()["detail"]
    assert "an issuer" in detail and "a client id" in detail and "a client secret" in detail


async def test_one_request_may_configure_and_turn_on_together(client, session, admin) -> None:
    """The form sends the provider and the mode at once, which must not be a chicken and egg."""
    answer = await _save(
        client,
        oidc_issuer=ISSUER,
        oidc_client_id="bindery",
        oidc_client_secret=SECRET,
        sso_mode="optional",
    )
    assert answer.status_code == 200
    assert answer.json()["sso_mode"] == "optional"
    assert await settings_store.get(session, settings_store.SSO_MODE) == "optional"


async def test_turning_it_on_later_reads_what_is_already_stored(client, admin) -> None:
    await _save(client, oidc_issuer=ISSUER, oidc_client_id="bindery", oidc_client_secret=SECRET)
    assert (await _save(client, sso_mode="required")).status_code == 200


@pytest.mark.parametrize(
    "issuer",
    [
        "auth.example.test",  # no scheme: not something an ID token can be verified against
        "http://auth.example.test",  # http: the token would cross the network in the clear
        "https://auth.example.test/?x=1",  # a query is not part of an issuer
    ],
)
async def test_a_bad_issuer_is_refused_now_rather_than_at_the_callback(
    client, admin, issuer
) -> None:
    answer = await _save(client, oidc_issuer=issuer)
    assert answer.status_code == 422


async def test_a_refused_mode_leaves_nothing_written(client, session, admin) -> None:
    """Half-applied configuration is the state where the screen says one thing and the front
    door does another."""
    answer = await _save(client, oidc_client_id="bindery", sso_mode="required")
    assert answer.status_code == 422
    assert await settings_store.get(session, settings_store.OIDC_CLIENT_ID) is None


async def test_omitting_the_secret_leaves_the_stored_one_alone(client, session, admin) -> None:
    await _save(client, oidc_issuer=ISSUER, oidc_client_id="bindery", oidc_client_secret=SECRET)
    await _save(client, oidc_client_id="bindery-2")

    assert await settings_store.get(session, settings_store.OIDC_CLIENT_SECRET) == SECRET


async def test_the_audit_says_which_provider_it_was_before(client, session, admin) -> None:
    """The question a swapped provider raises, and the one the audit has to answer."""
    import sqlalchemy as sa

    from api.db.models import AuditEvent

    await _save(client, oidc_issuer=ISSUER, oidc_client_id="bindery", oidc_client_secret=SECRET)
    await _save(client, oidc_issuer="https://auth.elsewhere.test")

    rows = (
        await session.execute(
            sa.select(AuditEvent)
            .where(AuditEvent.action == "update_settings")
            .order_by(AuditEvent.created_at.desc())
        )
    ).scalars().all()
    assert rows[0].before["oidc_issuer"] == ISSUER
    assert "oidc_issuer" in rows[0].after["changed"]
    # And never the secret itself, in either half.
    assert SECRET not in str(rows[0].before) + str(rows[0].after)
