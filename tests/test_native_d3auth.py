"""D3 Auth tokens for D3 Constellation (BND-T-22.3): a token audienced at this Bindery stands in
for a session once its identity is linked, and the link is made with Bindery's own credentials."""

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient

from api import settings_store
from api.auth import d3auth_bearer
from tests.conftest import PASSWORD

ISSUER = "https://auth.example.test"
RESOURCE = "https://testserver"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {
    **jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True),
    "kid": "k1",
    "alg": "RS256",
}


def token(sub: str, *, aud: str = RESOURCE, iss: str = ISSUER, ttl: int = 300, key=KEY) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "sub": sub,
        "aud": aud,
        "iat": now,
        "exp": now + ttl,
        "scope": "openid d3:roles",
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def provider_keys(monkeypatch, client):
    async def fetch(issuer: str) -> dict:
        assert issuer == ISSUER
        return {"keys": [JWK]}

    monkeypatch.setattr(d3auth_bearer, "fetch_jwks", fetch)
    d3auth_bearer._jwks_cache.clear()
    # Each test from its own address, so deliberate failures throttle nothing else.
    client.headers["cf-connecting-ip"] = f"198.51.100.{uuid.uuid4().int % 250 + 1}"


async def configure(session, mode: str = "optional") -> None:
    for key, value in (
        (settings_store.OIDC_ISSUER, ISSUER),
        (settings_store.OIDC_CLIENT_ID, "bindery"),
        (settings_store.OIDC_CLIENT_SECRET, "a-secret"),
        (settings_store.SSO_MODE, mode),
    ):
        await settings_store.set_(session, key, value, actor_id=None)
    await session.commit()


def bearer(value: str) -> dict:
    return {"Authorization": f"Bearer {value}"}


async def test_the_manifest_names_d3_auth_only_when_it_is_configured(
    client: AsyncClient, session
) -> None:
    await configure(session, mode="off")
    off = (await client.get("/.well-known/d3-app.json")).json()
    assert "d3auth" not in off["signIn"]["methods"] and off["endpoints"]["link"] is None
    await configure(session)
    on = (await client.get("/.well-known/d3-app.json")).json()
    assert "d3auth" in on["signIn"]["methods"]
    assert on["signIn"]["d3auth"] == {"issuer": ISSUER, "resource": RESOURCE}
    assert on["endpoints"]["link"] == f"{RESOURCE}/api/auth/native/link"


async def test_an_unlinked_identity_is_asked_to_link_then_gets_in(
    client: AsyncClient, session, user_factory
) -> None:
    await configure(session)
    user, _ = await user_factory()
    sub = f"person-{uuid.uuid4().hex[:6]}"
    access = token(sub)

    me = await client.get("/api/auth/native/me", headers=bearer(access))
    assert me.status_code == 401
    assert me.json()["type"] == "https://d3cloud.io/problems/identity_not_linked"

    wrong = await client.post(
        "/api/auth/native/link",
        headers=bearer(access),
        json={"email": user.email, "password": "not-it"},
    )
    assert wrong.json()["type"].endswith("/invalid_credentials")

    linked = await client.post(
        "/api/auth/native/link",
        headers=bearer(access),
        json={"email": user.email, "password": PASSWORD},
    )
    assert linked.status_code == 200, linked.text

    me = await client.get("/api/auth/native/me", headers=bearer(access))
    assert me.status_code == 200 and me.json()["email"] == user.email
    # And the ordinary API, which is what the app's screens call.
    assert (await client.get("/api/auth/me", headers=bearer(access))).status_code == 200

    again = await client.post(
        "/api/auth/native/link",
        headers=bearer(access),
        json={"email": user.email, "password": PASSWORD},
    )
    assert again.status_code == 409, "a link is made once"


@pytest.mark.parametrize(
    "why",
    ["another audience", "another issuer", "expired", "someone else's key"],
)
async def test_a_token_not_for_this_bindery_is_refused(
    client: AsyncClient, session, user_factory, why: str
) -> None:
    await configure(session)
    user, _ = await user_factory()
    sub = f"person-{uuid.uuid4().hex[:6]}"
    good = token(sub)
    assert (
        await client.post(
            "/api/auth/native/link",
            headers=bearer(good),
            json={"email": user.email, "password": PASSWORD},
        )
    ).status_code == 200
    bad = {
        "another audience": token(sub, aud="https://foreman.example.test"),
        "another issuer": token(sub, iss="https://evil.example.test"),
        "expired": token(sub, ttl=-120),
        "someone else's key": token(
            sub, key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ),
    }[why]
    assert (await client.get("/api/auth/native/me", headers=bearer(bad))).status_code == 401
    assert (await client.get("/api/auth/me", headers=bearer(bad))).status_code == 401


async def test_with_sso_off_a_provider_token_is_nothing(client: AsyncClient, session) -> None:
    await configure(session, mode="off")
    me = await client.get("/api/auth/native/me", headers=bearer(token("anyone")))
    assert me.status_code == 401 and me.json()["type"].endswith("/session_revoked")
