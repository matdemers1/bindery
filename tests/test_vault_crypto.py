"""The key hierarchy (T-16.1, REQ-177, ADR-012).

This module is what the irreversible part of the feature rests on. T-16.5
deletes plaintext once the ciphertext verifies, so "encrypt then decrypt
returns exactly the input" is not a nice property here — it is the difference
between a vault and a shredder.
"""

import os

import pytest

from api.vault import crypto


def unlocked(passphrase="a-long-enough-passphrase", pin="481516", pepper=None):
    """A vault as it exists after setup: one data key, wrapped twice."""
    pepper = pepper or crypto.new_pepper()
    dek = crypto.new_data_key()
    p_salt, n_salt = os.urandom(16), os.urandom(16)
    by_passphrase = crypto.wrap(dek, crypto.derive_from_passphrase(passphrase, p_salt), p_salt)
    by_pin = crypto.wrap(dek, crypto.derive_from_pin(pin, n_salt, pepper), n_salt)
    return dek, by_passphrase, by_pin, pepper


@pytest.mark.parametrize(
    "payload",
    [b"", b"a", b"\x00" * 1024, os.urandom(4096), "unicode — ✓ — bytes".encode()],
)
def test_encrypt_then_decrypt_returns_exactly_the_input(payload):
    key = crypto.new_data_key()
    assert crypto.decrypt(crypto.encrypt(payload, key), key) == payload


def test_the_same_plaintext_twice_is_two_different_ciphertexts():
    """A fresh nonce each time. Identical ciphertexts would say two vaulted
    documents are the same document without decrypting either."""
    key = crypto.new_data_key()
    assert crypto.encrypt(b"a payslip", key) != crypto.encrypt(b"a payslip", key)


def test_tampering_is_detected_rather_than_returning_wrong_bytes():
    """AES-GCM is authenticated, so a flipped bit fails loudly. Silently
    returning corrupted plaintext is the one outcome a vault cannot have."""
    key = crypto.new_data_key()
    blob = bytearray(crypto.encrypt(b"the deed", key))
    blob[-1] ^= 0x01
    with pytest.raises(crypto.WrongSecret):
        crypto.decrypt(bytes(blob), key)


def test_both_secrets_unwrap_the_same_data_key():
    dek, by_passphrase, by_pin, pepper = unlocked()
    from_pass = crypto.unwrap(
        by_passphrase, crypto.derive_from_passphrase("a-long-enough-passphrase", by_passphrase.salt)
    )
    from_pin = crypto.unwrap(
        by_pin, crypto.derive_from_pin("481516", by_pin.salt, pepper)
    )
    assert from_pass == from_pin == dek


def test_the_pin_is_useless_without_the_host_pepper():
    """REQ-178, and the reason a six-digit PIN is defensible at all.

    Someone holding a stolen database or the S3 copy has the wrapped key and the
    salt. Without the pepper — which is excluded from the backup, the export and
    offsite replication — the right PIN still does not unwrap it.
    """
    _, _, by_pin, _ = unlocked(pin="481516")
    with pytest.raises(crypto.WrongSecret):
        crypto.unwrap(by_pin, crypto.derive_from_pin("481516", by_pin.salt, crypto.new_pepper()))


def test_a_wrong_pin_fails_the_same_way_a_corrupt_wrapper_does():
    """Telling them apart would tell an attacker which half they had right."""
    _, _, by_pin, pepper = unlocked(pin="481516")
    with pytest.raises(crypto.WrongSecret):
        crypto.unwrap(by_pin, crypto.derive_from_pin("000000", by_pin.salt, pepper))


def test_destroying_the_pin_wrapper_leaves_the_passphrase_one_working():
    """The PIN copy is a convenience. It can be thrown away and rebuilt after
    too many failures without re-encrypting a single file."""
    dek, by_passphrase, _, _ = unlocked()
    assert crypto.unwrap(
        by_passphrase, crypto.derive_from_passphrase("a-long-enough-passphrase", by_passphrase.salt)
    ) == dek


def test_every_file_gets_its_own_key():
    dek = crypto.new_data_key()
    assert crypto.file_key(dek, b"one") != crypto.file_key(dek, b"two")
    assert crypto.file_key(dek, b"one") == crypto.file_key(dek, b"one")


def test_one_files_key_does_not_open_another():
    dek = crypto.new_data_key()
    blob = crypto.encrypt(b"a passport", crypto.file_key(dek, b"one"))
    with pytest.raises(crypto.WrongSecret):
        crypto.decrypt(blob, crypto.file_key(dek, b"two"))


def test_a_short_passphrase_is_refused():
    with pytest.raises(crypto.VaultError, match="at least"):
        crypto.derive_from_passphrase("short", os.urandom(16))


@pytest.mark.parametrize("pin", ["12", "abcdef", "", "1234567890123", "12 34"])
def test_an_implausible_pin_is_refused(pin):
    with pytest.raises(crypto.VaultError):
        crypto.derive_from_pin(pin, os.urandom(16), crypto.new_pepper())


def test_a_truncated_pepper_is_refused():
    """A missing pepper file read as empty bytes would silently weaken every
    PIN in the archive, and nothing else would notice."""
    with pytest.raises(crypto.VaultError, match="pepper"):
        crypto.derive_from_pin("481516", os.urandom(16), b"")


def test_the_wrapper_survives_a_round_trip_through_json():
    """It is stored as JSON in the database; hex rather than raw bytes."""
    _, by_passphrase, _, _ = unlocked()
    assert crypto.Wrapped.from_dict(by_passphrase.as_dict()) == by_passphrase


def test_associated_data_binds_ciphertext_to_its_context():
    """So a vaulted blob cannot be moved onto another document's row and still
    decrypt — the row it belongs to is authenticated along with the bytes."""
    key = crypto.new_data_key()
    blob = crypto.encrypt(b"a will", key, associated=b"document-a")
    assert crypto.decrypt(blob, key, associated=b"document-a") == b"a will"
    with pytest.raises(crypto.WrongSecret):
        crypto.decrypt(blob, key, associated=b"document-b")
