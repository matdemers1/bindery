"""The backup reads back what it wrote (CR-072).

Two failures look alike at the byte level and must not be treated alike: an
original that has rotted in the source pool, and a copy that did not survive
the write. The first is not a backup failure — `--force` exists to take a
generation anyway, and aborting would lose the documents that are still good.
"""

import os
from pathlib import Path

import pytest

from api.export import backup
from api.storage.blobs import blob_path


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    from api.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _seed(sha: str, content: bytes) -> Path:
    path = blob_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.chmod(path, 0o444)
    return path


def test_a_healthy_blob_is_copied_and_counted(data_root, tmp_path_factory) -> None:
    import hashlib

    content = b"%PDF-1.7 an original\n"
    sha = hashlib.sha256(content).hexdigest()
    _seed(sha, content)

    out = tmp_path_factory.mktemp("gen")
    count, total = backup.copy_blobs(out / "blobs")
    assert count == 1
    assert total == len(content)


def test_a_rotted_original_is_backed_up_rather_than_aborting(data_root, tmp_path_factory) -> None:
    """The generation being taken to preserve the other eight hundred documents
    must not be stopped by the one that has already decayed."""
    import hashlib

    good = b"%PDF-1.7 intact\n"
    good_sha = hashlib.sha256(good).hexdigest()
    _seed(good_sha, good)

    # A blob whose bytes no longer hash to its own name: bit rot.
    rotted_sha = hashlib.sha256(b"what this used to be\n").hexdigest()
    _seed(rotted_sha, b"decayed on disk\n")

    out = tmp_path_factory.mktemp("gen")
    count, _ = backup.copy_blobs(out / "blobs")
    assert count == 2, "the rotted original must still reach the backup"

    copied = out / "blobs" / rotted_sha[:2] / rotted_sha[2:4] / rotted_sha
    assert copied.read_bytes() == b"decayed on disk\n"


def test_a_copy_that_does_not_read_back_raises(data_root, tmp_path_factory, monkeypatch) -> None:
    """A failing or full disk. Louder than a backup that reports success."""
    import hashlib

    content = b"%PDF-1.7 an original\n"
    sha = hashlib.sha256(content).hexdigest()
    _seed(sha, content)

    def truncating_copy(src, dst, *args, **kwargs):
        Path(dst).write_bytes(b"")

    monkeypatch.setattr(backup.shutil, "copy2", truncating_copy)

    with pytest.raises(RuntimeError, match="did not verify"):
        backup.copy_blobs(tmp_path_factory.mktemp("gen") / "blobs")
