"""Unlocking, and what a wrong PIN costs (T-16.1 to T-16.3, REQ-176, REQ-178).

A six-digit PIN is 20 bits. Everything here exists because that is not very
much: the pepper keeps a stolen database from being enough to attack it
offline, and the failure counter keeps a stolen *host* from being enough to
attack it online. Neither one is allowed to lose data when it fires.
"""

import uuid

import pytest
import sqlalchemy as sa

from api.db.models import Vault
from api.vault import crypto, pepper, service
from api.vault.session import sessions

PASSPHRASE = "correct horse battery staple"
PIN = "481516"


@pytest.fixture
async def vault(session, signed_in, tmp_path, monkeypatch):
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    user, _ = await signed_in()
    made = await service.create(session, user.id, PASSPHRASE, PIN)
    await session.commit()
    return user, made


async def test_both_wrappers_open_the_same_key(session, vault):
    """One data key wrapped twice — not two vaults that happen to agree."""
    _user, made = vault
    by_pin = await service.unlock_with_pin(session, made, PIN)
    by_passphrase = await service.unlock_with_passphrase(session, made, PASSPHRASE)
    assert by_pin == by_passphrase


async def test_a_wrong_pin_is_counted(session, vault):
    _user, made = vault
    with pytest.raises(crypto.WrongSecret):
        await service.unlock_with_pin(session, made, "000000")
    assert made.pin_failures == 1


async def test_a_good_pin_clears_the_count(session, vault):
    _user, made = vault
    for _ in range(3):
        with pytest.raises(crypto.WrongSecret):
            await service.unlock_with_pin(session, made, "000000")
    await service.unlock_with_pin(session, made, PIN)
    assert made.pin_failures == 0


async def test_five_wrong_pins_destroy_the_wrapper_and_nothing_else(session, vault):
    """The lockout must cost the *PIN*, never the documents.

    Wiping the vault on five wrong entries would turn a toddler with a phone
    into data loss.
    """
    _user, made = vault
    for _ in range(service.MAX_PIN_FAILURES):
        with pytest.raises(crypto.WrongSecret):
            await service.unlock_with_pin(session, made, "000000")

    assert made.pin_wrapped is None
    with pytest.raises(crypto.VaultError):
        await service.unlock_with_pin(session, made, PIN)
    # Everything is still readable.
    assert await service.unlock_with_passphrase(session, made, PASSPHRASE)


async def test_a_new_pin_after_lockout_opens_the_same_key(session, vault):
    """Recovery re-wraps; it does not re-encrypt. If this returned a different
    key, every sealed object would already be unreadable."""
    _user, made = vault
    before = await service.unlock_with_passphrase(session, made, PASSPHRASE)
    for _ in range(service.MAX_PIN_FAILURES):
        with pytest.raises(crypto.WrongSecret):
            await service.unlock_with_pin(session, made, "000000")

    await service.set_pin(session, made, before, "902100")
    assert made.pin_failures == 0
    assert await service.unlock_with_pin(session, made, "902100") == before


async def test_the_database_alone_cannot_open_the_pin_wrapper(session, vault, tmp_path):
    """The threat this defends: someone with a database dump and no host.

    Without the pepper file, twenty bits of PIN would fall to a laptop in
    minutes. Deriving with the right PIN and the wrong pepper must fail.
    """
    _user, made = vault
    wrapped = crypto.Wrapped.from_dict(made.pin_wrapped)
    stolen_host = b"\x00" * 32
    assert stolen_host != pepper.load_or_create()

    with pytest.raises(crypto.WrongSecret):
        crypto.unwrap(wrapped, crypto.derive_from_pin(PIN, wrapped.salt, stolen_host))


async def test_the_key_is_never_written_to_the_database(session, vault):
    """A wrapped key is the point; a plaintext one would defeat all of it."""
    user, made = vault
    key = await service.unlock_with_passphrase(session, made, PASSPHRASE)
    row = (
        await session.execute(
            sa.select(Vault).where(Vault.user_id == user.id)
        )
    ).scalar_one()
    written = repr(row.passphrase_wrapped) + repr(row.pin_wrapped)
    assert key.hex() not in written
    assert PASSPHRASE not in written
    assert PIN not in written


async def test_one_vault_per_account(session, vault):
    user, _made = vault
    with pytest.raises(crypto.VaultError, match="already has a vault"):
        await service.create(session, user.id, PASSPHRASE, PIN)


async def test_locking_forgets_the_key(session, vault):
    user, made = vault
    await service.unlock_with_pin(session, made, PIN)
    assert service.require_key(user.id)
    sessions.lock(user.id)
    with pytest.raises(service.VaultLocked):
        service.require_key(user.id)


async def test_one_users_unlock_does_not_open_anothers(session, vault, signed_in):
    """Vaults are per account (the user's answer to the open question), and an
    in-process session cache is exactly where that could quietly stop being
    true."""
    _user, made = vault
    other, _ = await signed_in(email=f"other-{uuid.uuid4().hex[:8]}@example.test")
    await service.unlock_with_pin(session, made, PIN)

    with pytest.raises(service.VaultLocked):
        service.require_key(other.id)
