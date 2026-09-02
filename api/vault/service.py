"""Setting up, unlocking and locking a vault (T-16.3, REQ-179).

The failure policy lives here, and it is the part worth reading.

A wrong PIN is counted. Past a threshold the **PIN-wrapped copy of the data key
is destroyed** — not the vault, not the documents, and not the passphrase copy.
The vault stays fully openable with the passphrase, and a new PIN can be set
afterwards without re-encrypting a single file. That is the whole reason the key
is wrapped twice.

Destroying the wrapper rather than locking the account is deliberate: an
attacker who can keep guessing has the database, and a counter they can reset by
restoring a row protects nothing. Removing the material they are guessing
against does.
"""

import logging
import os
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Vault
from api.vault import crypto, pepper
from api.vault.session import sessions

log = logging.getLogger("bindery.vault")

# Five. Enough for a mistyped PIN and a bad day; far short of a million.
MAX_PIN_FAILURES = 5


class VaultLocked(Exception):
    """The caller asked for something the vault cannot answer while locked."""


async def get(session: AsyncSession, user_id: uuid.UUID) -> Vault | None:
    return (
        await session.execute(sa.select(Vault).where(Vault.user_id == user_id))
    ).scalar_one_or_none()


async def create(
    session: AsyncSession, user_id: uuid.UUID, passphrase: str, pin: str
) -> Vault:
    """One data key, wrapped twice, and the vault is open from here."""
    if await get(session, user_id) is not None:
        raise crypto.VaultError("this account already has a vault")

    data_key = crypto.new_data_key()
    host_pepper = pepper.load_or_create()

    passphrase_salt, pin_salt = os.urandom(crypto.SALT_BYTES), os.urandom(crypto.SALT_BYTES)
    # Both derivations go through the bounded pool: half a gigabyte and a couple
    # of seconds of blocking C, which must not happen on the event loop.
    passphrase_key = await crypto.derive_from_passphrase_async(passphrase, passphrase_salt)
    pin_key = await crypto.derive_from_pin_async(pin, pin_salt, host_pepper)
    vault = Vault(
        user_id=user_id,
        passphrase_wrapped=crypto.wrap(data_key, passphrase_key, passphrase_salt).as_dict(),
        pin_wrapped=crypto.wrap(data_key, pin_key, pin_salt).as_dict(),
    )
    session.add(vault)
    await session.flush()
    sessions.unlock(user_id, data_key)
    log.info("vault created for %s", user_id)
    return vault


async def unlock_with_pin(session: AsyncSession, vault: Vault, pin: str) -> bytes:
    if vault.pin_wrapped is None:
        raise crypto.WrongSecret(
            "PIN unlock is disabled for this vault after too many wrong attempts. "
            "Use the passphrase, and you can set a new PIN afterwards."
        )
    wrapped = crypto.Wrapped.from_dict(vault.pin_wrapped)
    pin_key = await crypto.derive_from_pin_async(pin, wrapped.salt, pepper.load_or_create())
    try:
        data_key = crypto.unwrap(wrapped, pin_key)
    except crypto.WrongSecret:
        vault.pin_failures += 1
        if vault.pin_failures >= MAX_PIN_FAILURES:
            # The wrapper goes, not the vault. Everything stays readable with
            # the passphrase and a new PIN can be set without re-encrypting.
            vault.pin_wrapped = None
            log.warning(
                "destroyed the PIN wrapper for %s after %s wrong attempts — the "
                "passphrase still opens it", vault.user_id, vault.pin_failures,
            )
        raise

    vault.pin_failures = 0
    vault.unlocked_at = datetime.now(UTC)
    sessions.unlock(vault.user_id, data_key)
    return data_key


async def unlock_with_passphrase(
    session: AsyncSession, vault: Vault, passphrase: str
) -> bytes:
    wrapped = crypto.Wrapped.from_dict(vault.passphrase_wrapped)
    passphrase_key = await crypto.derive_from_passphrase_async(passphrase, wrapped.salt)
    data_key = crypto.unwrap(wrapped, passphrase_key)
    vault.pin_failures = 0
    vault.unlocked_at = datetime.now(UTC)
    sessions.unlock(vault.user_id, data_key)
    return data_key


async def set_pin(session: AsyncSession, vault: Vault, data_key: bytes, pin: str) -> None:
    """Re-wrap the existing key under a new PIN. Nothing is re-encrypted."""
    salt = os.urandom(crypto.SALT_BYTES)
    pin_key = await crypto.derive_from_pin_async(pin, salt, pepper.load_or_create())
    vault.pin_wrapped = crypto.wrap(data_key, pin_key, salt).as_dict()
    vault.pin_failures = 0


def require_key(user_id: uuid.UUID) -> bytes:
    key = sessions.key(user_id)
    if key is None:
        raise VaultLocked("the vault is locked")
    return key
