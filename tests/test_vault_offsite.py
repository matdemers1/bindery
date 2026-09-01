"""What leaves the building, and what must not (T-16.10, REQ-186).

Vault objects are the only files in the archive with no second source. A blob
can be re-scanned or re-downloaded; a sealed object exists once. So the thing
worth testing is not that the vault works but that a copy of it gets out — and
that the one file which must *not* get out, doesn't.
"""

import hashlib
import json
import pathlib

import pytest

from api import offsite
from api.export import backup as local_backup
from api.offsite import VAULT_PREFIX, vault_object_key
from api.vault import pepper, store
from tests.test_offsite_sync import CONFIG, FakeS3


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _seal_two(data_root):
    """Two sealed objects and a pepper, without needing the whole pipeline."""
    names = [store.new_object_name() for _ in range(2)]
    for name in names:
        path = store.object_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ciphertext for " + name.encode())
    pepper.load_or_create()
    return names


def test_the_backup_carries_the_sealed_objects(data_root, tmp_path):
    """Without this, a restore brings back vault rows pointing at ciphertext no
    backup ever held — the most protected part of the archive would be the only
    part with no copy."""
    names = _seal_two(data_root)
    destination = tmp_path / "backup" / "vault"

    count, total = local_backup.copy_vault(destination)

    assert count == 2
    assert total > 0
    for name in names:
        copied = destination / "objects" / name[:2] / name[2:4] / name
        assert copied.is_file(), f"{name[:12]} did not reach the backup"
        assert copied.read_bytes() == store.object_path(name).read_bytes()


def test_the_backup_carries_the_pepper(data_root, tmp_path):
    """The local backup sits on the host that already has the pepper, so
    including it changes no threat model and makes a same-host restore whole."""
    _seal_two(data_root)
    destination = tmp_path / "backup" / "vault"

    local_backup.copy_vault(destination)

    carried = destination / pepper.PEPPER_NAME
    assert carried.is_file()
    assert carried.read_bytes() == pepper.load_or_create()
    assert oct(carried.stat().st_mode)[-3:] == "600"


def test_a_vault_with_nothing_in_it_is_not_an_error(data_root, tmp_path):
    """Most accounts will never make one, and a backup must not fail over it."""
    count, total = local_backup.copy_vault(tmp_path / "backup" / "vault")
    assert (count, total) == (0, 0)


def test_the_offsite_key_is_not_the_blob_key(data_root):
    """A vault object's name is random, not a content address. Filing it under
    `blobs/` would tell the reconcile the key describes the bytes, which for
    this one kind of object is false."""
    name = store.new_object_name()
    key = vault_object_key(name)
    assert key.startswith(VAULT_PREFIX)
    assert key == f"vault/{name[:2]}/{name[2:4]}/{name}"


def test_the_manifest_says_what_cannot_be_read(data_root, tmp_path, monkeypatch):
    """A restore reads the manifest first. Finding out then that part of the
    backup will not open is finding out too late."""
    _seal_two(data_root)
    monkeypatch.setattr(
        local_backup,
        "dump_database",
        lambda path: (path.write_bytes(b"dump"), path)[1],
    )
    monkeypatch.setattr(local_backup, "copy_blobs", lambda destination: (0, 0))

    result = local_backup.run_backup(destination=tmp_path / "generation")

    manifest = json.loads((tmp_path / "generation" / "manifest.json").read_text())
    assert manifest["vault"]["object_count"] == 2
    assert manifest["vault"]["readable_without_passphrase"] is False
    assert manifest["vault"]["pepper_included"] is True
    assert "passphrase" in manifest["vault"]["note"]
    assert result.vault_object_count == 2


async def test_the_sealed_objects_reach_the_bucket(session, data_root):
    names = _seal_two(data_root)
    fake = FakeS3()

    result = await offsite.sync_vault_objects(
        session, CONFIG, fake, vault_root=store.vault_root()
    )

    assert result.uploaded == 2
    assert result.failures == []
    for name in names:
        assert vault_object_key(name) in fake.objects


async def test_the_pepper_never_reaches_the_bucket(session, data_root):
    """The one file that must stay in the building.

    It is what stops a stolen database from being enough to brute-force a
    six-digit PIN. A bucket holding the dump *and* the pepper is a bucket where
    that is no longer true. Excluded by where it is stored rather than by a
    filter, which is why this is asserted: someone widening `vault_root()` to
    the parent directory would start shipping it and nothing else would notice.
    """
    _seal_two(data_root)
    fake = FakeS3()

    await offsite.sync_vault_objects(
        session, CONFIG, fake, vault_root=store.vault_root()
    )

    secret = pepper.load_or_create()
    assert all(pepper.PEPPER_NAME not in key for key in fake.objects)
    assert all(body != secret for body in fake.objects.values())


async def test_a_half_written_object_is_not_shipped(session, data_root):
    """A `.partial` from an interrupted seal is not a vault object, and a key
    nothing can interpret is worse in a bucket with no delete permission."""
    _seal_two(data_root)
    stray = store.object_path(store.new_object_name())
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.with_suffix(".partial").write_bytes(b"half a ciphertext")
    fake = FakeS3()

    result = await offsite.sync_vault_objects(
        session, CONFIG, fake, vault_root=store.vault_root()
    )

    assert result.uploaded == 2
    assert all(not key.endswith(".partial") for key in fake.objects)


async def test_a_second_run_ships_nothing_twice(session, data_root):
    """Versioning is on and the credentials cannot delete, so a re-upload of an
    unchanged object leaves a version nothing can remove."""
    _seal_two(data_root)
    fake = FakeS3()

    first = await offsite.sync_vault_objects(
        session, CONFIG, fake, vault_root=store.vault_root()
    )
    second = await offsite.sync_vault_objects(
        session, CONFIG, fake, vault_root=store.vault_root()
    )

    assert (first.uploaded, first.skipped) == (2, 0)
    assert (second.uploaded, second.skipped) == (0, 2)


# --------------------------------------------------------------------------
# The drill covers a vaulted document, end to end (T-16.12, REQ-187)
# --------------------------------------------------------------------------

DRILL = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "restore-drill.sh"


def test_the_drill_does_not_ask_for_a_vaulted_original():
    """Sealing a document deletes its plaintext. A drill that still expects it
    would fail a backup that is in fact perfect — and a drill that cries wolf
    is one nobody runs."""
    body = DRILL.read_text()
    assert "d.vaulted_by IS NOT NULL" in body, (
        "the blob check does not exclude vaulted documents, so it will report "
        "MISSING BLOB for every one of them"
    )


def test_the_drill_checks_the_sealed_objects_came_back():
    """Excluding them from the blob check is only half of it. Skipping them
    entirely would mean the one part of the archive with no second copy is the
    one part the drill never looks at."""
    body = DRILL.read_text()
    assert "MISSING VAULT OBJECT" in body
    assert "offsite-vault" in body, "the offsite drill does not check the vault"
    assert "vault_item" in body


async def test_a_sealed_object_that_comes_back_wrong_is_caught(session, data_root):
    """The check that a presence test cannot make.

    A vault object's name says nothing about its bytes, so the only thing that
    can catch a corrupted one is the digest the ledger recorded when it went up
    — which the restore brings back alongside it.
    """
    from api import offsite

    names = _seal_two(data_root)
    fake = FakeS3()
    await offsite.sync_vault_objects(
        session, CONFIG, fake, vault_root=store.vault_root()
    )

    wanted = [
        (name, hashlib.sha256(fake.objects[vault_object_key(name)]).hexdigest())
        for name in names
    ]
    good = offsite.fetch_vault_objects(fake, CONFIG, wanted, data_root / "drill")
    assert (good.fetched, good.corrupt, good.missing) == (2, [], [])

    # Now the bucket hands back different bytes under the same key.
    fake.objects[vault_object_key(names[0])] = b"not what went up"
    bad = offsite.fetch_vault_objects(fake, CONFIG, wanted, data_root / "drill2")
    assert bad.corrupt == [names[0]]
    assert bad.fetched == 1


async def test_a_sealed_object_missing_from_the_bucket_is_caught(session, data_root):
    from api import offsite

    names = _seal_two(data_root)
    fake = FakeS3()
    await offsite.sync_vault_objects(
        session, CONFIG, fake, vault_root=store.vault_root()
    )
    wanted = [
        (name, hashlib.sha256(fake.objects[vault_object_key(name)]).hexdigest())
        for name in names
    ]
    del fake.objects[vault_object_key(names[1])]

    result = offsite.fetch_vault_objects(fake, CONFIG, wanted, data_root / "drill")
    assert result.missing == [names[1]]
