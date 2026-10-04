"""The D3 App contract for native clients (BND-T-22.1, BND-T-22.2): the manifest, native sessions
in JSON, problem+json refusals — and that a native session is an ordinary Bindery session
underneath."""

from datetime import UTC, datetime

import jwt
import sqlalchemy as sa
from httpx import AsyncClient

from api.auth.totp import _code_for_step, current_step, new_secret
from api.db.models import AppUser, AuditEvent, RefreshToken
from tests.conftest import PASSWORD

PROBLEM = "application/problem+json"
# Failures from the shared test address would throttle every later test's sign-in, so the tests
# that fail on purpose do it from an address of their own.
OWN_ADDRESS = {"CF-Connecting-IP": "203.0.113.76"}
DEVICE = {"name": "Matthew's iPhone", "platform": "ios"}


def problem_type(response) -> str:
    assert response.headers["content-type"].startswith(PROBLEM), response.text
    return response.json()["type"].removeprefix("https://d3cloud.io/problems/")


async def with_authenticator(session, user: AppUser) -> str:
    user.totp_secret = new_secret()
    user.totp_confirmed_at = datetime.now(UTC)
    await session.commit()
    return user.totp_secret


async def sign_in(client: AsyncClient, user: AppUser, secret: str, *, step_offset: int = 0) -> dict:
    first = await client.post(
        "/api/auth/native/signin",
        json={"email": user.email, "password": PASSWORD, "device": DEVICE},
    )
    assert first.status_code == 202, first.text
    assert first.json()["next"] == "totp"
    code = _code_for_step(secret, current_step() + step_offset)
    second = await client.post(
        "/api/auth/native/signin", json={"challenge": first.json()["challenge"], "totp": code}
    )
    assert second.status_code == 200, second.text
    return second.json()


async def test_the_manifest_names_the_native_endpoints(client: AsyncClient) -> None:
    response = await client.get("/.well-known/d3-app.json")
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["product"] == "bindery" and manifest["contract"] == 1
    assert "bindery.search" in manifest["capabilities"]
    assert set(manifest["signIn"]["methods"]) >= {"password", "totp"}
    endpoints = manifest["endpoints"]
    assert endpoints["nativeSignIn"] == "https://testserver/api/auth/native/signin"
    assert {endpoints[k] for k in ("nativeRefresh", "nativeRevoke", "me")} == {
        "https://testserver/api/auth/native/refresh",
        "https://testserver/api/auth/native/revoke",
        "https://testserver/api/auth/native/me",
    }


async def test_password_then_code_gives_a_named_session(
    client: AsyncClient, session, user_factory
) -> None:
    user, _ = await user_factory()
    secret = await with_authenticator(session, user)
    body = await sign_in(client, user, secret)
    # The contract's ceiling, whatever the browser's access tokens are set to.
    assert body["expiresIn"] <= 15 * 60 and body["session"]["id"]
    claims = jwt.decode(body["accessToken"], options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] <= 15 * 60, "the token itself lives no longer than it says"

    me = await client.get(
        "/api/auth/native/me", headers={"Authorization": f"Bearer {body['accessToken']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == user.email and me.json()["roles"] == ["member"]

    # The same Bearer token reaches the ordinary API: a native session is a session.
    assert (
        await client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['accessToken']}"})
    ).status_code == 200

    row = (
        await session.execute(sa.select(RefreshToken).where(RefreshToken.user_id == user.id))
    ).scalar_one()
    assert (row.device_name, row.device_platform) == ("Matthew's iPhone", "ios")
    audit = (
        await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.entity_id == user.id, AuditEvent.action == "login"
            )
        )
    ).scalar_one()
    assert audit.after["via"] == "native"


async def test_an_account_without_an_authenticator_signs_in_at_once(
    client: AsyncClient, user_factory
) -> None:
    user, _ = await user_factory()
    response = await client.post(
        "/api/auth/native/signin",
        json={"email": user.email, "password": PASSWORD, "device": DEVICE},
    )
    assert response.status_code == 200 and response.json()["accessToken"]


async def test_refusals_are_registered_problems(client: AsyncClient, session, user_factory) -> None:
    user, _ = await user_factory()
    secret = await with_authenticator(session, user)
    wrong = await client.post(
        "/api/auth/native/signin",
        json={"email": user.email, "password": "nope", "device": DEVICE},
        headers=OWN_ADDRESS,
    )
    assert wrong.status_code == 401 and problem_type(wrong) == "invalid_credentials"
    unknown = await client.post(
        "/api/auth/native/signin",
        json={"email": "nobody@example.test", "password": "nope"},
        headers=OWN_ADDRESS,
    )
    assert problem_type(unknown) == "invalid_credentials", (
        "an unknown address reads exactly like a wrong password"
    )

    first = await client.post(
        "/api/auth/native/signin",
        json={"email": user.email, "password": PASSWORD, "device": DEVICE},
    )
    right = _code_for_step(secret, current_step())
    bad = str((int(right) + 1) % 1_000_000).zfill(6)
    code = await client.post(
        "/api/auth/native/signin",
        json={"challenge": first.json()["challenge"], "totp": bad},
        headers=OWN_ADDRESS,
    )
    assert code.status_code == 401 and problem_type(code) == "invalid_code"
    forged = await client.post(
        "/api/auth/native/signin",
        json={"challenge": "not-a-challenge", "totp": right},
        headers=OWN_ADDRESS,
    )
    assert problem_type(forged) == "invalid_code"

    me = await client.get("/api/auth/native/me")
    assert me.status_code == 401 and problem_type(me) == "session_revoked"


async def test_refresh_rotates_and_reuse_ends_the_session(
    client: AsyncClient, session, user_factory
) -> None:
    user, _ = await user_factory()
    secret = await with_authenticator(session, user)
    body = await sign_in(client, user, secret)

    rotated = await client.post(
        "/api/auth/native/refresh", json={"refreshToken": body["refreshToken"]}
    )
    assert rotated.status_code == 200
    assert rotated.json()["refreshToken"] != body["refreshToken"]

    replay = await client.post(
        "/api/auth/native/refresh", json={"refreshToken": body["refreshToken"]}
    )
    assert replay.status_code == 401 and problem_type(replay) == "refresh_reused"
    after = await client.post(
        "/api/auth/native/refresh", json={"refreshToken": rotated.json()["refreshToken"]}
    )
    assert after.status_code == 401 and problem_type(after) in {"refresh_reused", "session_revoked"}
    me = await client.get(
        "/api/auth/native/me", headers={"Authorization": f"Bearer {rotated.json()['accessToken']}"}
    )
    assert me.status_code == 401, "reuse ends the access token too"


async def test_revoke_ends_the_session(client: AsyncClient, session, user_factory) -> None:
    user, _ = await user_factory()
    secret = await with_authenticator(session, user)
    body = await sign_in(client, user, secret)
    revoked = await client.post(
        "/api/auth/native/revoke", headers={"Authorization": f"Bearer {body['accessToken']}"}
    )
    assert revoked.status_code == 204
    again = await client.post(
        "/api/auth/native/refresh", json={"refreshToken": body["refreshToken"]}
    )
    assert again.status_code == 401 and problem_type(again) == "session_revoked"


async def test_native_sign_in_is_throttled_like_the_web(client: AsyncClient, user_factory) -> None:
    user, _ = await user_factory()
    statuses = []
    for _ in range(25):
        # An address of its own, so the failures don't throttle every other test's sign-in.
        response = await client.post(
            "/api/auth/native/signin",
            json={"email": user.email, "password": "wrong"},
            headers={"CF-Connecting-IP": "203.0.113.77"},
        )
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert problem_type(response) == "throttled"
            assert isinstance(response.json()["retryAfter"], int)
            break
    assert 429 in statuses, statuses


def test_nginx_sends_the_manifest_to_the_api() -> None:
    """The contract fixes the path at the origin's root, outside /api — so nginx must route it, or
    the web app's index.html answers in its place (which is what a live check found)."""
    from pathlib import Path

    conf = (Path(__file__).resolve().parent.parent / "infra" / "nginx.conf").read_text()
    start = conf.index("location = /.well-known/d3-app.json {")
    block = conf[start : conf.index("}", start)]
    assert "proxy_pass http://$api_upstream:8000$request_uri;" in block
    assert "X-Forwarded-Proto $forwarded_scheme" in block
