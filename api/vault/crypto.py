"""Key hierarchy and file encryption for the vault (T-16.1, REQ-177, ADR-012).

Deliberately knows nothing about the database, HTTP, or documents. It takes
bytes and secrets and returns bytes, which is what makes it testable on its own
— and this is the module the irreversible part of the feature depends on, so
"testable on its own" is not a stylistic preference.

    passphrase --Argon2id--> KEK ----+
                                     +--> DEK --> per-file keys --> AES-256-GCM
    PIN + host pepper --Argon2id--> PEK

One random data key, wrapped twice. The passphrase copy is the durable one; the
PIN copy is a convenience that can be destroyed and rebuilt without re-encrypting
a single file.
"""

import hmac
import os
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from api.auth import kdf

KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
PEPPER_BYTES = 32

# Argon2id, sized for the two very different jobs these do.
#
# The passphrase is entered rarely and guards everything, so it gets the
# expensive parameters. The PIN is entered daily *and* is only six digits — a
# million guesses — so its cost is what stands between an attacker holding the
# host and the data. It is deliberately as slow as a human will tolerate once,
# because a machine will do it 10^6 times.
PASSPHRASE_COST = {"time_cost": 4, "memory_cost": 256 * 1024, "parallelism": 4}
PIN_COST = {"time_cost": 6, "memory_cost": 256 * 1024, "parallelism": 4}

MIN_PASSPHRASE = 12
MIN_PIN = 4
MAX_PIN = 12


class VaultError(Exception):
    """Something about the vault is wrong in a way the caller must handle."""


class WrongSecret(VaultError):
    """The passphrase or PIN did not unwrap the data key.

    Indistinguishable from a corrupted wrapper on purpose — AES-GCM
    authentication fails the same way for both, and telling them apart would
    tell an attacker which of the two they had got right.
    """


@dataclass(frozen=True)
class Wrapped:
    """A data key encrypted under something derived from a human secret."""

    salt: bytes
    nonce: bytes
    ciphertext: bytes

    def as_dict(self) -> dict[str, str]:
        return {
            "salt": self.salt.hex(),
            "nonce": self.nonce.hex(),
            "ciphertext": self.ciphertext.hex(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> "Wrapped":
        return cls(
            salt=bytes.fromhex(raw["salt"]),
            nonce=bytes.fromhex(raw["nonce"]),
            ciphertext=bytes.fromhex(raw["ciphertext"]),
        )


def new_data_key() -> bytes:
    """The key everything is ultimately encrypted under. Generated once, ever."""
    return os.urandom(KEY_BYTES)


def new_pepper() -> bytes:
    return os.urandom(PEPPER_BYTES)


def _derive(secret: bytes, salt: bytes, cost: dict) -> bytes:
    return hash_secret_raw(
        secret=secret, salt=salt, hash_len=KEY_BYTES, type=Type.ID, **cost
    )


def derive_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < MIN_PASSPHRASE:
        raise VaultError(
            f"a vault passphrase must be at least {MIN_PASSPHRASE} characters — "
            "it is the only thing standing between a stolen backup and these files"
        )
    return _derive(passphrase.encode(), salt, PASSPHRASE_COST)


async def derive_from_passphrase_async(passphrase: str, salt: bytes) -> bytes:
    """`derive_from_passphrase`, off the event loop and behind the bounded pool.

    256 MiB and about a second of blocking C, which is exactly what it is for —
    and exactly why it must not run inside an `async def` handler. Called
    inline, one unlock stopped every other request in the process for that
    second, and several at once allocated a gigabyte at a time on a host whose
    OOM killer would take Postgres with it. See `api/auth/kdf.py`.

    The length check happens here, on the event loop, so a refusal never
    occupies a slot in the pool.
    """
    if len(passphrase) < MIN_PASSPHRASE:
        raise VaultError(
            f"a vault passphrase must be at least {MIN_PASSPHRASE} characters — "
            "it is the only thing standing between a stolen backup and these files"
        )
    return await kdf.derive(_derive, passphrase.encode(), salt, PASSPHRASE_COST)


async def derive_from_pin_async(pin: str, salt: bytes, pepper: bytes) -> bytes:
    """`derive_from_pin`, off the event loop. Same reasoning as above."""
    if not pin.isdigit() or not MIN_PIN <= len(pin) <= MAX_PIN:
        raise VaultError(f"a PIN is {MIN_PIN} to {MAX_PIN} digits")
    if len(pepper) < PEPPER_BYTES:
        raise VaultError("the host pepper is missing or truncated")
    secret = hmac.new(pepper, pin.encode(), "sha256").digest()
    return await kdf.derive(_derive, secret, salt, PIN_COST)


def derive_from_pin(pin: str, salt: bytes, pepper: bytes) -> bytes:
    """PIN plus a secret that never leaves the host (REQ-178).

    The pepper is the whole reason this is defensible. A PIN is at most a few
    million guesses and a lockout counter protects nothing against someone
    holding a disk image, because they are not asking the application. Mixing in
    a value that is excluded from the backup, the export and the offsite copy
    means a stolen database yields no PIN-derivable key at all.
    """
    if not pin.isdigit() or not MIN_PIN <= len(pin) <= MAX_PIN:
        raise VaultError(f"a PIN is {MIN_PIN} to {MAX_PIN} digits")
    if len(pepper) < PEPPER_BYTES:
        raise VaultError("the host pepper is missing or truncated")
    # Mixed with HMAC rather than concatenated: `pin + pepper` invites the
    # length-extension and ambiguity problems that come free with a keyed hash.
    secret = hmac.new(pepper, pin.encode(), "sha256").digest()
    return _derive(secret, salt, PIN_COST)


def wrap(data_key: bytes, wrapping_key: bytes, salt: bytes) -> Wrapped:
    nonce = os.urandom(NONCE_BYTES)
    return Wrapped(
        salt=salt,
        nonce=nonce,
        ciphertext=AESGCM(wrapping_key).encrypt(nonce, data_key, None),
    )


def unwrap(wrapped: Wrapped, wrapping_key: bytes) -> bytes:
    try:
        return AESGCM(wrapping_key).decrypt(wrapped.nonce, wrapped.ciphertext, None)
    except Exception as error:  # InvalidTag, and anything else the primitive raises
        raise WrongSecret("that did not unlock the vault") from error


def file_key(data_key: bytes, file_id: bytes) -> bytes:
    """A distinct key per file, derived rather than stored.

    Deriving means there is no second thing to keep, lose or leak — and every
    file getting its own key means a nonce reused by a bug damages one file
    rather than revealing a relationship between two.
    """
    return hmac.new(data_key, b"bindery-vault-file:" + file_id, "sha256").digest()


def encrypt(plaintext: bytes, key: bytes, *, associated: bytes = b"") -> bytes:
    """Nonce-prefixed AES-256-GCM. Authenticated, so tampering is detected."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, associated or None)


def decrypt(blob: bytes, key: bytes, *, associated: bytes = b"") -> bytes:
    if len(blob) <= NONCE_BYTES:
        raise WrongSecret("that is too short to be vault ciphertext")
    nonce, body = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, body, associated or None)
    except Exception as error:
        raise WrongSecret("this ciphertext did not decrypt with that key") from error
