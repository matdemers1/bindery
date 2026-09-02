"""The attacks CR-114 … CR-119 describe, written as requests.

Each test here is one attacker's sequence: what they send, what the server did
before, and what they got. They are grouped by the finding they close, and every
one of them failed before the change it names.

The style is `tests/test_permission_boundary.py`'s — assert on what a caller
*receives*, not on which branch the code took — because a boundary asserted
through internals survives a refactor that moves the boundary.

Addresses are given explicitly with `CF-Connecting-IP` wherever a test provokes
failures on purpose. `api/auth/throttle.py` counts per address across every
account, and the whole suite shares one database: a test that spends the free
attempts on the transport's default address makes the *next* test's login a 429.
"""

import asyncio
import threading

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from api.auth.cookies import ACCESS_COOKIE
from api.db.enums import MembershipRole
from api.db.models import LoginAttempt, RefreshToken, Vault
from api.main import app
from api.vault import guard
from tests.conftest import PASSWORD

VAULT_PASSPHRASE = "correct horse battery staple"
VAULT_PIN = "481516"


def _bare() -> AsyncClient:
    """A client with no cookies, for the second party in an attack."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")


# ---------------------------------------------------------------------------
# CR-114 — the vault passphrase had unlimited online guesses, each of them a
# 256 MiB Argon2 on the event loop
# ---------------------------------------------------------------------------


async def test_the_vault_passphrase_cannot_be_guessed_without_limit(
    client, signed_in, session, monkeypatch
):
    """The attack: sign in, then POST /api/vault/unlock with a passphrase, over
    and over. The PIN path counts failures and destroys its wrapper at five; the
    passphrase path counted nothing, returned 401 and committed, so the loop ran
    forever against the one secret in the design that has no reset — "losing
    this passphrase loses the documents".

    Now the attempt past the limit is refused *before* any derivation, and the
    refusal says how long for. The limit is lowered here only to keep the test
    from spending ten seconds of deliberate Argon2; the mechanism is the same
    one at the shipped value.
    """
    user, _ = await signed_in()
    monkeypatch.setattr(guard, "MAX_ATTEMPTS", 3)
    headers = {"cf-connecting-ip": "192.0.2.140"}

    setup = await client.post(
        "/api/vault/setup", json={"passphrase": VAULT_PASSPHRASE, "pin": VAULT_PIN}
    )
    assert setup.status_code == 200, setup.text
    # Creating a vault leaves it open; the attack starts from a shut one.
    assert (await client.post("/api/vault/lock")).status_code == 200

    for attempt in range(guard.MAX_ATTEMPTS):
        wrong = await client.post(
            "/api/vault/unlock",
            json={"passphrase": f"wrong passphrase {attempt}"},
            headers=headers,
        )
        assert wrong.status_code == 401, wrong.text

    refused = await client.post(
        "/api/vault/unlock", json={"passphrase": "wrong passphrase again"}, headers=headers
    )
    assert refused.status_code == 429, refused.text
    assert int(refused.headers["retry-after"]) >= 1

    # And the refusal is a refusal, not a removal: destroying the passphrase
    # wrapper would destroy the vault, so this limit expires rather than acts.
    vault = (
        await session.execute(sa.select(Vault).where(Vault.user_id == user.id))
    ).scalar_one()
    assert vault.passphrase_wrapped is not None
    state = (await client.get("/api/vault")).json()
    assert state["exists"] is True and state["unlocked"] is False


async def test_the_vault_derivation_runs_off_the_event_loop(monkeypatch):
    """Availability. 256 MiB and about a second of blocking C called inline from
    an `async def` handler stops every other request in the process — every
    search, every upload, the health poll — and several at once is a gigabyte at
    a time on a host whose OOM killer takes Postgres with it.

    Asserted by the thread it ran on, which is the property rather than a
    stopwatch: the bounded pool in `api/auth/kdf.py`, not the loop, and not the
    default executor's thirty-two.
    """
    from api.auth import kdf
    from api.vault import crypto

    ran_on: list[str] = []
    real = crypto._derive

    def spy(*args, **kwargs):
        ran_on.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(crypto, "_derive", spy)
    await crypto.derive_from_passphrase_async("a long enough passphrase", b"0" * 16)

    assert ran_on and ran_on[0].startswith("bindery-kdf"), ran_on
    assert kdf.MAX_CONCURRENT <= 2


async def test_a_refused_vault_unlock_costs_no_derivation(client, signed_in, monkeypatch):
    """The throttle is only worth having if it comes *before* the expensive part
    — otherwise the refusal is itself the denial of service.
    """
    await signed_in()
    monkeypatch.setattr(guard, "MAX_ATTEMPTS", 1)
    headers = {"cf-connecting-ip": "192.0.2.141"}

    await client.post(
        "/api/vault/setup", json={"passphrase": VAULT_PASSPHRASE, "pin": VAULT_PIN}
    )
    assert (
        await client.post(
            "/api/vault/unlock", json={"passphrase": "not the right passphrase"}, headers=headers
        )
    ).status_code == 401

    from api.vault import crypto

    derivations: list[int] = []
    real = crypto._derive
    monkeypatch.setattr(
        crypto, "_derive", lambda *a, **k: (derivations.append(1), real(*a, **k))[1]
    )

    refused = await client.post(
        "/api/vault/unlock", json={"passphrase": "still not the right passphrase"}, headers=headers
    )
    assert refused.status_code == 429
    assert derivations == []


# ---------------------------------------------------------------------------
# CR-115 — the login throttle could not see a burst, and Argon2 ran on the loop
# ---------------------------------------------------------------------------


async def test_concurrent_login_attempts_are_all_counted(user_factory):
    """The attack: fire the password guesses at once rather than in sequence.

    `check` counted rows and `record` wrote one, with nothing between them, so
    every request in a burst read the same pre-attack count, every one saw zero
    failures, and every one proceeded. The throttle was built for a sequential
    attacker and did not see a burst at all — which also meant N concurrent
    Argon2 verifications at 64 MiB each.

    The advisory lock in `throttle.check` is what closes it: the second request
    from an address waits for the first to commit its attempt row, so the count
    it reads is the true one.
    """
    user, _ = await user_factory()
    address = "203.0.113.77"
    burst = 6

    async def guess(n: int) -> int:
        async with _bare() as attacker:
            response = await attacker.post(
                "/api/auth/login",
                json={"email": user.email, "password": f"wrong-{n}"},
                headers={"cf-connecting-ip": address},
            )
            return response.status_code

    codes = await asyncio.gather(*(guess(n) for n in range(burst)))
    assert all(code in (401, 429) for code in codes), codes
    # The one that matters. Before the lock, all six read a count of zero and
    # all six were answered 401; the burst was invisible to the backoff. Now
    # they queue, so the one that arrives past the free attempts is refused.
    assert 429 in codes, codes

    # The burst spent the free attempts instead of slipping through on a stale
    # count of zero, so the next guess from that address is refused.
    async with _bare() as probe:
        after = await probe.post(
            "/api/auth/login",
            json={"email": user.email, "password": "wrong-again"},
            headers={"cf-connecting-ip": address},
        )
    assert after.status_code == 429, after.text


async def test_password_verification_runs_off_the_event_loop(monkeypatch):
    """Same reasoning as the vault's, one order of magnitude down — and reachable
    without an account at all, because the decoy hash for an unknown address does
    exactly the same work.
    """
    from api.auth import passwords

    ran_on: list[str] = []
    real = passwords.verify_password

    def spy(password_hash: str, password: str) -> bool:
        ran_on.append(threading.current_thread().name)
        return real(password_hash, password)

    monkeypatch.setattr(passwords, "verify_password", spy)
    await passwords.verify_password_async(passwords.decoy_hash(), "not the decoy")

    assert ran_on and ran_on[0].startswith("bindery-kdf"), ran_on


# ---------------------------------------------------------------------------
# CR-117 — X-Forwarded-For's first element is the client's to choose
# ---------------------------------------------------------------------------


async def test_the_client_cannot_choose_its_own_throttle_key(client, user_factory, session):
    """The attack: no CF-Connecting-IP, and a fresh `X-Forwarded-For` per guess.

    nginx sets `$proxy_add_x_forwarded_for`, which *appends* the peer it saw to
    whatever the client sent — so the first element is attacker-supplied text and
    the last is the one a proxy in this deployment wrote. Reading the first meant
    a password-spraying client sent `10.0.0.1`, then `10.0.0.2`, and the per-IP
    backoff — the half that actually stops a password list — never counted two
    failures against the same key.
    """
    user, _ = await user_factory()

    for n in range(4):
        response = await client.post(
            "/api/auth/login",
            json={"email": user.email, "password": f"nope-{n}"},
            headers={"x-forwarded-for": f"10.0.0.{n}, 198.51.100.44"},
        )
        assert response.status_code in (401, 429), response.text

    keys = (
        (
            await session.execute(
                sa.select(LoginAttempt.ip).where(
                    LoginAttempt.email == user.email.lower(),
                    LoginAttempt.succeeded.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    # One key, not four: the last hop, which the client did not write.
    assert set(keys) == {"198.51.100.44"}, keys


async def test_the_edge_header_still_wins(client, user_factory, session):
    """CF-Connecting-IP is what the tunnel sets, and it is preferred over the
    chain rather than merely consulted first."""
    user, _ = await user_factory()
    await client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "nope"},
        headers={
            "cf-connecting-ip": "192.0.2.155",
            "x-forwarded-for": "10.0.0.1, 198.51.100.44",
        },
    )
    keys = (
        (
            await session.execute(
                sa.select(LoginAttempt.ip).where(LoginAttempt.email == user.email.lower())
            )
        )
        .scalars()
        .all()
    )
    assert keys == ["192.0.2.155"]


# ---------------------------------------------------------------------------
# CR-118 — GET /api/settings handed the offsite destination to everyone
# ---------------------------------------------------------------------------

_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_BUCKET = "household-archive-offsite"


async def test_a_reader_cannot_read_the_offsite_destination(
    client, session, signed_in, user_factory
):
    """The attack: sign in as the least-privileged account there is — a READER
    who owns nothing — and GET /api/settings.

    The write path has been the administrator's since SEC-02. The read path
    asked for nothing beyond being signed in, and returned the bucket, the
    region, the KMS key, the IAM access key id and the last four characters of
    the secret: where the offsite copy of everyone's documents lives, and a
    four-character check on the credential for it.
    """
    admin, _ = await signed_in()
    admin.is_admin = True
    await session.commit()
    saved = await client.put(
        "/api/settings",
        json={
            "aws_access_key_id": _KEY_ID,
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "offsite_bucket": _BUCKET,
            "offsite_region": "us-east-1",
            "offsite_kms_key_id": "alias/bindery-offsite",
        },
    )
    assert saved.status_code == 200, saved.text

    reader, _ = await user_factory(library_name="Read Only", role=MembershipRole.READER)
    async with _bare() as reader_client:
        signed = await reader_client.post(
            "/api/auth/login", json={"email": reader.email, "password": PASSWORD}
        )
        assert signed.status_code == 200, signed.text
        body = (await reader_client.get("/api/settings")).json()

    assert body["aws_access_key_id"] is None
    assert body["aws_secret_hint"] is None
    assert body["aws_secret_configured"] is False
    assert body["offsite_bucket"] is None
    assert body["offsite_region"] is None
    assert body["offsite_kms_key_id"] is None
    # And nothing about the destination came back through another field.
    assert _BUCKET not in str(body)
    assert _KEY_ID not in str(body)


async def test_the_administrator_still_sees_the_offsite_destination(
    client, session, signed_in
):
    """The redaction must not break the panel it exists to protect."""
    admin, _ = await signed_in()
    admin.is_admin = True
    await session.commit()

    await client.put(
        "/api/settings", json={"aws_access_key_id": _KEY_ID, "offsite_bucket": _BUCKET}
    )
    body = (await client.get("/api/settings")).json()
    assert body["aws_access_key_id"] == _KEY_ID
    assert body["offsite_bucket"] == _BUCKET


# ---------------------------------------------------------------------------
# CR-116 — the notification webhook was an unrestricted server-side fetch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///data/blobs/ab/cdef",
        "ftp://198.51.100.9/upload",
        "gopher://198.51.100.9:5432/_x",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:5432/",
        "http://localhost:8000/api/settings",
        "http://[::1]:8000/",
        "https://",
    ],
)
async def test_the_webhook_refuses_anything_that_is_not_a_webhook(client, signed_in, url):
    """The attack: save a notification URL that is not a notification service.

    `api/notify.py` POSTs this from inside the api and worker containers with
    `urllib.request.urlopen`, which opens `file:` and `ftp:` as happily as
    `https:`. The only validation on the way in was `.strip()`, so any account
    that could save settings held a server-side request primitive aimed at the
    compose network, the LAN and the host's metadata endpoint — and the alert
    payload it carries names files.
    """
    await signed_in()
    response = await client.put("/api/settings", json={"notify_webhook_url": url})
    assert response.status_code == 422, response.text


async def test_a_real_webhook_is_still_accepted(client, signed_in):
    """Including one on the LAN. A self-hosted ntfy is the named use case in
    `api/notify.py`, so refusing private addresses would remove the feature
    rather than secure it.
    """
    await signed_in()
    for url in ("https://ntfy.sh/my-archive", "http://192.168.1.20:8080/bindery"):
        response = await client.put("/api/settings", json={"notify_webhook_url": url})
        assert response.status_code == 200, response.text
    # Empty still clears it — that is how notifications are turned off.
    cleared = await client.put("/api/settings", json={"notify_webhook_url": ""})
    assert cleared.status_code == 200
    assert cleared.json()["notify_webhook_configured"] is False


# ---------------------------------------------------------------------------
# CR-119 — access tokens outlived logout, password reset and suspension
# ---------------------------------------------------------------------------


async def test_signing_out_ends_the_session_for_a_stolen_access_token(client, signed_in):
    """The attack: capture the access cookie, wait for the owner to sign out,
    keep using it.

    Logout revoked the refresh token and deleted the browser's copy of the
    cookies. The access token was a signed JWT that nothing consulted — no
    session id, no server-side state — so it kept working for the rest of its
    thirty minutes. On a shared or borrowed machine, "sign out" ended nothing.
    """
    await signed_in()
    stolen = client.cookies[ACCESS_COOKIE]

    carried = {"Cookie": f"{ACCESS_COOKIE}={stolen}"}
    async with _bare() as thief:
        assert (await thief.get("/api/auth/me", headers=carried)).status_code == 200

        assert (await client.post("/api/auth/logout")).status_code == 204

        assert (await thief.get("/api/auth/me", headers=carried)).status_code == 401


async def test_changing_the_password_ends_a_stolen_access_token(client, signed_in):
    """The scenario the control exists for. Somebody has your session, so you
    change your password — and they stayed signed in for up to half an hour,
    because `revoke_other_sessions` updates refresh-token rows and the access
    token never looked at one.
    """
    await signed_in()
    stolen = client.cookies[ACCESS_COOKIE]

    carried = {"Cookie": f"{ACCESS_COOKIE}={stolen}"}
    async with _bare() as thief:
        assert (await thief.get("/api/auth/me", headers=carried)).status_code == 200

        changed = await client.post(
            "/api/account/password",
            json={
                "current_password": PASSWORD,
                "new_password": "a-brand-new-long-password",
            },
        )
        assert changed.status_code == 204, changed.text

        assert (await thief.get("/api/auth/me", headers=carried)).status_code == 401


async def test_logout_revokes_even_without_the_refresh_cookie(client, signed_in, session):
    """A client that did not send the refresh cookie — its path is `/api/auth`,
    so anything that lost it, or never had it — used to get a sign-out that
    revoked nothing at all.
    """
    user, _ = await signed_in()
    access = client.cookies[ACCESS_COOKIE]

    async with _bare() as bare:
        signed_out = await bare.post(
            "/api/auth/logout", headers={"Cookie": f"{ACCESS_COOKIE}={access}"}
        )
        assert signed_out.status_code == 204

    live = (
        (
            await session.execute(
                sa.select(RefreshToken).where(
                    RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert live == []


async def test_a_refreshed_session_keeps_working(client, signed_in):
    """Rotation mints a new access token bound to the new refresh row. If the
    binding were wrong in either direction, every refresh would sign the user out
    — so this is the test that keeps the fix from being a lockout.
    """
    await signed_in()
    assert (await client.post("/api/auth/refresh")).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200
