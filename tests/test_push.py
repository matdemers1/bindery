"""Push to D3 Constellation through the relay (BND-T-23.4, the D3 App contract's push).

A device registers its relay and key with a native session and gets one bindery.registered
notification sealed to its key and signed with the send key; the registration follows the session
through its rotations and is forgotten — never deleted — once the session ends, or the relay answers
410. The contract's reference envelopes open with this implementation.
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import AsyncClient

from api import push
from api.config import get_settings
from api.db.models import RelayRegistration
from tests.test_native_contract import sign_in, with_authenticator

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "envelope-v1.json").read_text())


class _Relay(BaseHTTPRequestHandler):
    pushes: ClassVar[list[dict]] = []
    answer = 202

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length", "0"))).decode()
        _Relay.pushes.append(
            {
                "path": self.path,
                "timestamp": self.headers.get("x-d3-relay-timestamp"),
                "signature": self.headers.get("x-d3-relay-signature"),
                "raw": raw,
            }
        )
        self.send_response(_Relay.answer)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture(scope="module")
def relay_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Relay)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(autouse=True)
def loopback_relay(monkeypatch):
    monkeypatch.setattr(get_settings(), "relay_allow_loopback_http", True)


def _device() -> tuple[ec.EllipticCurvePrivateKey, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return key, base64.b64encode(raw).decode()


def _push_to(registration: str) -> dict:
    found = [p for p in _Relay.pushes if p["path"] == f"/v1/push/{registration}"]
    assert found, f"no push for {registration}"
    return found[-1]


def test_the_contract_vectors_open() -> None:
    d = int.from_bytes(base64.urlsafe_b64decode(VECTORS["devicePrivateKeyD"] + "=="), "big")
    device = ec.derive_private_key(d, ec.SECP256R1())
    for case in VECTORS["cases"]:
        assert json.loads(push.open_envelope(device, case["envelope"])) == case["payload"]


def test_sealing_is_fresh_and_only_the_device_opens_it() -> None:
    key, raw = _device()
    a = push.seal(base64.b64decode(raw), b'{"v":1}')
    assert a != push.seal(base64.b64decode(raw), b'{"v":1}')
    assert push.open_envelope(key, a) == b'{"v":1}'
    with pytest.raises(Exception):  # noqa: B017 — any failure: it must not open
        push.open_envelope(ec.generate_private_key(ec.SECP256R1()), a)


async def test_register_then_one_notification_only_this_device_opens(
    client: AsyncClient, session, user_factory, relay_url
) -> None:
    user, _ = await user_factory()
    secret = await with_authenticator(session, user)
    tokens = await sign_in(client, user, secret)
    key, raw = _device()
    registration = f"reg-{tokens['session']['id']}"
    res = await client.post(
        "/api/push/native/register",
        headers={"Authorization": f"Bearer {tokens['accessToken']}"},
        json={
            "devicePublicKey": raw,
            "relay": {
                "url": relay_url,
                "registration": registration,
                "sendKey": "send-key-0123456789abc",
            },
            "categories": [],
        },
    )
    assert res.status_code == 204, res.text
    pushed = _push_to(registration)
    assert pushed["signature"] == push.sign(
        "send-key-0123456789abc", pushed["timestamp"], pushed["raw"]
    )
    payload = json.loads(push.open_envelope(key, json.loads(pushed["raw"])["ciphertext"]))
    assert payload["category"] == "bindery.registered" and payload["v"] == 1
    row = (
        await session.execute(
            sa.select(RelayRegistration).where(RelayRegistration.registration == registration)
        )
    ).scalar_one()
    assert "send-key" not in row.send_key_sealed

    manifest = (await client.get("/.well-known/d3-app.json")).json()
    assert manifest["endpoints"]["relayRegister"] == "https://testserver/api/push/native/register"


async def test_refusals(client: AsyncClient, session, user_factory, relay_url) -> None:
    user, _ = await user_factory()
    secret = await with_authenticator(session, user)
    tokens = await sign_in(client, user, secret)
    _, raw = _device()
    bearer = {"Authorization": f"Bearer {tokens['accessToken']}"}
    no_relay = await client.post(
        "/api/push/native/register",
        headers=bearer,
        json={"devicePublicKey": raw, "apnsToken": "ab" * 32},
    )
    assert no_relay.status_code == 400
    bad_key = await client.post(
        "/api/push/native/register",
        headers=bearer,
        json={
            "devicePublicKey": base64.b64encode(b"\x04" * 65).decode(),
            "relay": {"url": relay_url, "registration": "x", "sendKey": "send-key-0123456789abc"},
        },
    )
    assert bad_key.status_code == 400
    plain = await client.post(
        "/api/push/native/register",
        headers=bearer,
        json={
            "devicePublicKey": raw,
            "relay": {
                "url": "http://relay.example.com",
                "registration": "x",
                "sendKey": "send-key-0123456789abc",
            },
        },
    )
    assert plain.status_code == 400
    assert (await client.post("/api/push/native/register", json={})).status_code == 401


async def test_follows_rotations_and_is_forgotten_with_the_session_or_a_410(
    client: AsyncClient, session, user_factory, relay_url
) -> None:
    user, _ = await user_factory()
    secret = await with_authenticator(session, user)
    tokens = await sign_in(client, user, secret)
    _, raw = _device()
    registration = f"rot-{tokens['session']['id']}"
    await client.post(
        "/api/push/native/register",
        headers={"Authorization": f"Bearer {tokens['accessToken']}"},
        json={
            "devicePublicKey": raw,
            "relay": {
                "url": relay_url,
                "registration": registration,
                "sendKey": "send-key-0123456789abc",
            },
            "categories": ["bindery.ready"],
        },
    )
    rotated = (
        await client.post("/api/auth/native/refresh", json={"refreshToken": tokens["refreshToken"]})
    ).json()

    async def row() -> RelayRegistration:
        session.expire_all()
        return (
            await session.execute(
                sa.select(RelayRegistration).where(RelayRegistration.registration == registration)
            )
        ).scalar_one()

    note = {"v": 1, "category": "bindery.ready", "title": "x", "sentAt": "2026-10-05T00:00:00Z"}
    # Rotated: the chain leads to a live session, so it still sends.
    assert await push.push(session, await row(), note) == "sent"

    _Relay.answer = 410
    try:
        assert await push.push(session, await row(), note) == "gone"
    finally:
        _Relay.answer = 202
    assert (await row()).forgotten_at is not None

    # A second registration, then the session ends: forgotten at the next send, never deleted.
    registration2 = f"end-{tokens['session']['id']}"
    await client.post(
        "/api/push/native/register",
        headers={"Authorization": f"Bearer {rotated['accessToken']}"},
        json={
            "devicePublicKey": raw,
            "relay": {
                "url": relay_url,
                "registration": registration2,
                "sendKey": "send-key-0123456789abc",
            },
            "categories": [],
        },
    )
    await client.post(
        "/api/auth/native/revoke", headers={"Authorization": f"Bearer {rotated['accessToken']}"}
    )
    session.expire_all()
    second = (
        await session.execute(
            sa.select(RelayRegistration).where(RelayRegistration.registration == registration2)
        )
    ).scalar_one()
    second_id = second.id
    assert await push.push(session, second, note) == "gone"
    session.expire_all()
    assert (await session.get(RelayRegistration, second_id)).forgotten_at is not None
