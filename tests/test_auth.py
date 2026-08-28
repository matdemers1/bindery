"""Auth suite (REQ-103): login, refresh rotation, logout, rejection."""

import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from api.auth.cookies import ACCESS_COOKIE, REFRESH_COOKIE
from api.db.models import RefreshToken
from api.main import app
from tests.conftest import PASSWORD


async def test_login_sets_httponly_cookies(client: AsyncClient, user_factory) -> None:
    user, _ = await user_factory()

    response = await client.post(
        "/api/auth/login", json={"email": user.email, "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.json()["email"] == user.email
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        header = next(h for h in response.headers.get_list("set-cookie") if h.startswith(name))
        assert "HttpOnly" in header
        assert "Secure" in header
    # The refresh cookie only travels to the endpoints that rotate it.
    refresh_header = next(
        h for h in response.headers.get_list("set-cookie") if h.startswith(REFRESH_COOKIE)
    )
    assert "Path=/api/auth" in refresh_header


async def test_login_rejects_wrong_password(client: AsyncClient, user_factory) -> None:
    user, _ = await user_factory()
    response = await client.post(
        "/api/auth/login", json={"email": user.email, "password": "not-the-password"}
    )
    assert response.status_code == 401
    assert ACCESS_COOKIE not in client.cookies


async def test_login_rejects_unknown_email(client: AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login", json={"email": "nobody@example.test", "password": PASSWORD}
    )
    assert response.status_code == 401


async def test_login_is_case_insensitive_on_email(client: AsyncClient, user_factory) -> None:
    user, _ = await user_factory(email="Mixed.Case@example.test")
    response = await client.post(
        "/api/auth/login", json={"email": "MIXED.CASE@EXAMPLE.TEST", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["email"] == user.email


async def test_me_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_me_returns_the_signed_in_user(client: AsyncClient, signed_in) -> None:
    user, _ = await signed_in()
    response = await client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)


async def test_bearer_token_authenticates_without_a_browser(client: AsyncClient, signed_in) -> None:
    """REQ-105 — a token must work without the cookie flow."""
    user, _ = await signed_in()
    access = client.cookies[ACCESS_COOKIE]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as bare:
        response = await bare.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {access}"}
        )

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)


async def test_garbage_token_is_rejected(client: AsyncClient) -> None:
    response = await client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401


async def test_expired_token_is_rejected(client: AsyncClient, user_factory, monkeypatch) -> None:
    from api.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_access_ttl_minutes", -1)
    user, _ = await user_factory()
    await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    monkeypatch.setattr(settings, "jwt_access_ttl_minutes", 30)

    assert (await client.get("/api/auth/me")).status_code == 401


async def test_refresh_rotates_the_token(client: AsyncClient, signed_in, session) -> None:
    user, _ = await signed_in()
    original_refresh = client.cookies[REFRESH_COOKIE]

    response = await client.post("/api/auth/refresh")

    assert response.status_code == 200
    assert client.cookies[REFRESH_COOKIE] != original_refresh

    rows = (
        await session.execute(
            sa.select(RefreshToken).where(RefreshToken.user_id == user.id)
        )
    ).scalars().all()
    assert len(rows) == 2
    old = next(r for r in rows if r.revoked_at is not None)
    new = next(r for r in rows if r.revoked_at is None)
    # Revoked, not deleted — and the chain is recorded.
    assert old.replaced_by_id == new.id


async def test_refresh_without_a_token_is_rejected(client: AsyncClient) -> None:
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_replayed_refresh_token_ends_every_session(
    client: AsyncClient, signed_in, session
) -> None:
    """A revoked token coming back means the secret leaked. Burn the family."""
    user, _ = await signed_in()
    stolen = client.cookies[REFRESH_COOKIE]

    await client.post("/api/auth/refresh")  # rotates; `stolen` is now revoked

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as attacker:
        # Raw header rather than the cookie jar, so a domain-matching quirk
        # cannot make this pass for the wrong reason.
        replay = await attacker.post(
            "/api/auth/refresh", headers={"Cookie": f"{REFRESH_COOKIE}={stolen}"}
        )
    assert replay.status_code == 401
    assert replay.json()["detail"] == "refresh token has already been used"

    live = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        )
    ).scalar_one()
    assert live == 0

    # The legitimate holder's refresh token is dead too, which is the point.
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_logout_revokes_and_clears(client: AsyncClient, signed_in, session) -> None:
    user, _ = await signed_in()

    response = await client.post("/api/auth/logout")

    assert response.status_code == 204
    assert (await client.post("/api/auth/refresh")).status_code == 401

    live = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        )
    ).scalar_one()
    assert live == 0
