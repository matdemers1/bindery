"""The pepper, and the exclusions that make it worth having (T-16.2, REQ-178).

The pepper is what stands between a six-digit PIN and someone holding a stolen
backup. That only holds while it is genuinely absent from the things that leave
the machine, so those exclusions are asserted here rather than remembered.
"""

import os

import pytest

from api.vault import crypto, pepper


@pytest.fixture(autouse=True)
def data_root(tmp_path, monkeypatch):
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    yield tmp_path
    get_settings.cache_clear()


def test_it_is_created_on_first_use_and_stable_after(data_root):
    first = pepper.load_or_create()
    assert len(first) >= crypto.PEPPER_BYTES
    assert pepper.load_or_create() == first, "a second read must not mint a new one"


def test_it_is_written_unreadable_to_anyone_else(data_root):
    pepper.load_or_create()
    mode = oct(os.stat(pepper.pepper_path()).st_mode)[-3:]
    assert mode == "600", f"the pepper is {mode}; it must not be world- or group-readable"


def test_a_truncated_pepper_is_refused_rather_than_used(data_root):
    """Reading a short file as bytes would silently weaken every PIN in the
    archive, and nothing else in the system would notice."""
    pepper.load_or_create()
    pepper.pepper_path().write_bytes(b"too short")
    with pytest.raises(crypto.VaultError, match="truncated"):
        pepper.load_or_create()


def test_it_lives_outside_the_directory_backups_copy(data_root):
    """`backup.sh` and offsite replication both copy `blobs/` and nothing else.

    If the pepper were in there, a stolen backup would carry both halves of the
    PIN path and the whole argument for using a PIN collapses.
    """
    from api.config import get_settings

    pepper.load_or_create()
    blob_root = get_settings().blob_root.resolve()
    assert blob_root not in pepper.pepper_path().resolve().parents


def test_the_export_does_not_walk_it(data_root):
    """`make export` is the abandonment insurance and is meant to be copied
    somewhere else wholesale. It must not carry the pepper with it."""
    from api.export import archive_export

    source = (
        __import__("pathlib").Path(archive_export.__file__).read_text()
    )
    assert pepper.PEPPER_NAME not in source
    # And the export builds from documents, not from a directory walk of the
    # data root — which is why this holds. If that ever changes, this fails.
    assert "data_root" not in source or "rglob" not in source


def test_offsite_replication_only_ever_syncs_blobs():
    """The prefix the uploader walks is the blob root, so the pepper cannot be
    swept up by a change to what lives under the data root."""
    from api import offsite

    assert offsite.BLOB_PREFIX == "blobs/"
    source = (
        __import__("pathlib").Path(offsite.__file__).read_text()
    )
    assert pepper.PEPPER_NAME not in source
