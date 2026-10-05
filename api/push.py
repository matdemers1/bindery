"""Push to D3 Constellation through the relay (BND-T-23.4, d3-app-contract spec/push.md).

Every notification is sealed to a key the device made for this connection — envelope version 1,
a fresh ephemeral key each time — so neither the relay nor Apple can read it:

    shared   = ECDH(ephemeral private, device public)
    key      = HKDF-SHA256(shared, salt = ephemeral.pub ‖ device.pub, info = "d3-relay-envelope-v1")
    sealed   = AES-256-GCM(key, 12-byte nonce, plaintext, aad = "d3-relay-envelope-v1")
    envelope = base64(0x01 ‖ ephemeral.pub (65) ‖ nonce (12) ‖ ciphertext ‖ tag (16))

and posted to `<relay>/v1/push/<registration>`, signed with the registration's send key. A push is
best effort: nothing here may fail the request that caused it, and a failure is logged.

A registration is *forgotten*, never deleted (invariant 3): when its session has ended, when its
D3 Auth identity has been disconnected, or when the relay answers 410.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import sqlalchemy as sa
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import OidcIdentity, RefreshToken, RelayRegistration
from api.settings_store import _cipher

log = logging.getLogger("bindery.push")

INFO = b"d3-relay-envelope-v1"
VERSION = 1
POINT = 65


def _key(shared: bytes, ephemeral_pub: bytes, device_pub: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=ephemeral_pub + device_pub, info=INFO
    ).derive(shared)


def device_key(raw: bytes) -> ec.EllipticCurvePublicKey | None:
    """A P-256 public key in X9.63 uncompressed form (65 bytes, leading 0x04), or None."""
    if len(raw) != POINT or raw[0] != 0x04:
        return None
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        return None


def _raw(public: ec.EllipticCurvePublicKey) -> bytes:
    return public.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def seal(device_public_key: bytes, plaintext: bytes) -> str:
    device = device_key(device_public_key)
    if device is None:
        raise ValueError("not a P-256 device key")
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    ephemeral_pub = _raw(ephemeral.public_key())
    key = _key(ephemeral.exchange(ec.ECDH(), device), ephemeral_pub, device_public_key)
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, plaintext, INFO)  # ciphertext ‖ tag
    return base64.b64encode(bytes([VERSION]) + ephemeral_pub + nonce + sealed).decode()


def open_envelope(device: ec.EllipticCurvePrivateKey, envelope: str) -> bytes:
    """The other half, for tests and for the contract's reference vectors."""
    raw = base64.b64decode(envelope)
    if raw[0] != VERSION or len(raw) < 1 + POINT + 12 + 16:
        raise ValueError("not a version 1 envelope")
    ephemeral_pub = raw[1 : 1 + POINT]
    nonce = raw[1 + POINT : 1 + POINT + 12]
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ephemeral_pub)
    key = _key(device.exchange(ec.ECDH(), peer), ephemeral_pub, _raw(device.public_key()))
    return AESGCM(key).decrypt(nonce, raw[1 + POINT + 12 :], INFO)


def sign(send_key: str, timestamp: str, body: str) -> str:
    """base64url(HMAC-SHA256(sendKey, "<timestamp>.<body>")), as the relay checks it."""
    digest = hmac.new(send_key.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def register(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    identity_id: uuid.UUID | None,
    device_public_key: bytes,
    relay_url: str,
    registration: str,
    send_key: str,
    categories: list[str],
) -> RelayRegistration:
    """Store a registration, forgetting the session's earlier one and the same slot's holder."""
    now = datetime.now(UTC)
    earlier = [
        RelayRegistration.relay_url == relay_url,
        RelayRegistration.registration == registration,
    ]
    stale = sa.and_(*earlier)
    if session_id is not None:
        stale = sa.or_(stale, RelayRegistration.session_id == session_id)
    await session.execute(
        sa.update(RelayRegistration)
        .where(stale, RelayRegistration.forgotten_at.is_(None))
        .values(forgotten_at=now)
    )
    row = RelayRegistration(
        user_id=user_id,
        session_id=session_id,
        identity_id=identity_id,
        device_public_key=device_public_key,
        relay_url=relay_url,
        registration=registration,
        send_key_sealed=_cipher().encrypt(send_key.encode()).decode(),
        categories=categories,
    )
    session.add(row)
    await session.flush()
    return row


async def _owner_is_live(session: AsyncSession, row: RelayRegistration) -> bool:
    """Whether what signed the device in still stands: the session's chain to its live end, or the
    D3 Auth identity's link."""
    if row.identity_id is not None:
        identity = await session.get(OidcIdentity, row.identity_id)
        return identity is not None and identity.unlinked_at is None
    token_id = row.session_id
    for _ in range(10_000):  # a chain is as long as the session's rotations; never unbounded
        if token_id is None:
            return False
        token = await session.get(RefreshToken, token_id)
        if token is None:
            return False
        if token.replaced_by_id is None:
            return token.revoked_at is None and token.expires_at > datetime.now(UTC)
        token_id = token.replaced_by_id
    return False


async def push(
    session: AsyncSession,
    row: RelayRegistration,
    notification: dict[str, Any],
    *,
    collapse_id: str | None = None,
) -> str:
    """Send one notification: sent, gone (forgotten) or failed. Commits only a forgetting."""
    try:
        if row.forgotten_at is not None:
            return "gone"
        if not await _owner_is_live(session, row):
            row.forgotten_at = datetime.now(UTC)
            await session.commit()
            return "gone"
        send_key = _cipher().decrypt(row.send_key_sealed.encode()).decode()
        body = json.dumps(
            {
                "ciphertext": seal(row.device_public_key, json.dumps(notification).encode()),
                "priority": "high",
                **({"collapseId": collapse_id[:64]} if collapse_id else {}),
            }
        )
        timestamp = str(int(time.time()))
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{row.relay_url}/v1/push/{row.registration}",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-d3-relay-timestamp": timestamp,
                    "x-d3-relay-signature": sign(send_key, timestamp, body),
                },
            )
        if response.status_code == 410:
            row.forgotten_at = datetime.now(UTC)
            await session.commit()
            return "gone"
        if response.status_code >= 400:
            log.warning("relay refused a push: %s for %s", response.status_code, row.id)
            return "failed"
        return "sent"
    except Exception as exc:
        log.warning("push to %s failed: %s", row.id, exc)
        return "failed"
