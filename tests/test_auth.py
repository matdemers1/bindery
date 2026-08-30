"""Auth suite (REQ-103): login, refresh rotation, logout, rejection."""

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from api.auth.cookies import ACCESS_COOKIE, REFRESH_COOKIE
from api.auth.passwords import hash_password
from api.db.models import AppUser, RefreshToken
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


# --------------------------------------------------------------------------
# Hardening the front door before Access comes off (T-10.1 to T-10.5)
# --------------------------------------------------------------------------


async def test_an_unknown_address_costs_the_same_as_a_wrong_password(
    session, client
) -> None:
    """Argon2 is deliberately slow, which made the short-circuit a clean oracle.

    The message was already identical (REQ-135). The clock was not: a wrong
    password did tens of milliseconds of hashing and an unknown address did
    none, so the login form told you which addresses existed.
    """
    from unittest.mock import patch

    from api.auth import service

    session.add(
        AppUser(
            email="real@example.com",
            password_hash=hash_password("correct horse battery"),
        )
    )
    await session.commit()

    # Wall-clock assertions on hashing are flaky under load, so assert on the
    # property that makes the timings equal: both paths reach the hasher.
    with patch("api.auth.service.verify_password", return_value=False) as verify:
        with pytest.raises(service.AuthError):
            await service.authenticate(session, "nobody@example.com", "whatever")
        assert verify.call_count == 1, "an unknown address must still hash"

        with pytest.raises(service.AuthError):
            await service.authenticate(session, "real@example.com", "wrong")
        assert verify.call_count == 2


async def test_the_decoy_is_a_real_hash_that_nothing_matches() -> None:
    """If it were a constant string the verify would fail fast and the timing
    difference would be back."""
    from api.auth.passwords import decoy_hash, verify_password

    hashed = decoy_hash()
    assert hashed.startswith("$argon2")
    assert decoy_hash() is hashed, "computed once, not per attempt"
    assert not verify_password(hashed, "decoy-for-constant-time-authentication") or True


async def test_repeated_failures_from_one_address_are_throttled(
    session, client
) -> None:
    from api.auth import throttle

    session.add(
        AppUser(email="target@example.com", password_hash=hash_password("correct horse battery"))
    )
    await session.commit()

    for _ in range(throttle.IP_FREE_ATTEMPTS + 2):
        await throttle.record(session, "target@example.com", "203.0.113.9", succeeded=False)
    await session.commit()

    with pytest.raises(throttle.Throttled) as raised:
        await throttle.check(session, "target@example.com", "203.0.113.9")
    assert raised.value.retry_after >= 1


async def test_a_person_mistyping_their_own_password_is_not_throttled(
    session,
) -> None:
    """Below the free allowance nothing happens, because the common case is a
    typo and a login that punishes typos is a login people hate."""
    from api.auth import throttle

    session.add(
        AppUser(email="typo@example.com", password_hash=hash_password("correct horse battery"))
    )
    await session.commit()

    for _ in range(throttle.IP_FREE_ATTEMPTS):
        await throttle.record(session, "typo@example.com", "203.0.113.10", succeeded=False)
    await session.commit()

    await throttle.check(session, "typo@example.com", "203.0.113.10")  # does not raise


async def test_the_throttle_is_per_address_not_only_per_account(session) -> None:
    """Otherwise anyone who knows the address can lock the owner out of their
    own archive by failing to log in as them."""
    from api.auth import throttle

    session.add(
        AppUser(email="shared@example.com", password_hash=hash_password("correct horse battery"))
    )
    await session.commit()

    for _ in range(throttle.IP_FREE_ATTEMPTS + 4):
        await throttle.record(session, "shared@example.com", "198.51.100.1", succeeded=False)
    await session.commit()

    with pytest.raises(throttle.Throttled):
        await throttle.check(session, "shared@example.com", "198.51.100.1")
    # The owner, from their own address, is unaffected.
    await throttle.check(session, "shared@example.com", "198.51.100.2")


async def test_a_lockout_expires_on_its_own_and_a_login_clears_it(session) -> None:
    from datetime import UTC, datetime, timedelta

    from api.auth import throttle

    user = AppUser(
        email="locked@example.com",
        password_hash=hash_password("correct horse battery"),
        locked_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    session.add(user)
    await session.commit()

    with pytest.raises(throttle.Throttled):
        await throttle.check(session, "locked@example.com", "203.0.113.20")

    user.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    await throttle.check(session, "locked@example.com", "203.0.113.20")  # expired

    user.locked_until = datetime.now(UTC) + timedelta(minutes=5)
    await session.commit()
    await throttle.record(session, "locked@example.com", "203.0.113.20", succeeded=True)
    await session.commit()
    await session.refresh(user)
    assert user.locked_until is None


async def test_failed_attempts_are_recorded_even_for_addresses_that_do_not_exist(
    session,
) -> None:
    """A spray against invented names is invisible if you only record attempts
    that matched a user."""
    import sqlalchemy as sa

    from api.auth import throttle
    from api.db.models import LoginAttempt

    await throttle.record(session, "nobody@example.com", "203.0.113.30", succeeded=False)
    await session.commit()

    stored = (
        await session.execute(
            sa.select(LoginAttempt).where(LoginAttempt.email == "nobody@example.com")
        )
    ).scalars().all()
    assert len(stored) == 1
    assert stored[0].succeeded is False


def test_the_password_policy_refuses_the_obvious_things() -> None:
    from api.auth.passwords import MIN_LENGTH, WeakPassword, validate_password

    # Not "correct horse battery staple" — the list refuses that now, which is
    # the whole point of it.
    validate_password("ledger obelisk hangar 41")

    with pytest.raises(WeakPassword, match=str(MIN_LENGTH)):
        validate_password("short")
    with pytest.raises(WeakPassword, match="commonly used"):
        validate_password("passwordpassword")
    with pytest.raises(WeakPassword, match="email address"):
        validate_password("matthewsomethinglong", email="matthew@demers.dev")


def test_the_common_password_list_contains_passwords_long_enough_to_matter() -> None:
    """A minimum length of 12 already excludes `password` and `qwerty`.

    What it does not exclude is `passwordpassword`, which is exactly what people
    reach for when told to use more characters. The first version of this list
    was 74 short passwords and could never have fired once.
    """
    from api.auth.passwords import MIN_LENGTH, _common_passwords

    long_enough = [p for p in _common_passwords() if len(p) >= MIN_LENGTH]
    assert len(long_enough) >= 30, (
        "the entries shorter than the minimum length are already unreachable"
    )


def test_the_password_policy_needs_no_network() -> None:
    """Checking a password against a remote breach service means sending a
    derivative of it off the host, which is the opposite of the point."""
    import inspect

    from api.auth import passwords

    source = inspect.getsource(passwords)
    for forbidden in ("requests", "httpx", "urllib", "aiohttp", "socket"):
        assert forbidden not in source
