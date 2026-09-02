"""`derived_for` is the address half of the only function that deletes derived
files, so a name it accepts is a directory `purge_derived` will remove.
"""

import pytest

from api import artifacts


def test_a_digest_resolves_under_the_derived_root() -> None:
    sha = "a" * 64
    assert artifacts.derived_for(sha).root.name == sha


@pytest.mark.parametrize(
    "name",
    [
        "../../blobs",          # the blob store — every original in the archive
        "../vault",             # the sealed objects
        "..",
        "a" * 63,               # too short to be a digest
        "A" * 64,               # digests are lowercase hex
        "g" * 64,               # not hex
        "",
    ],
)
def test_anything_that_is_not_a_digest_is_refused(name: str) -> None:
    with pytest.raises(ValueError):
        artifacts.derived_for(name)


def test_the_purge_cannot_be_pointed_at_the_blob_store(tmp_path, monkeypatch) -> None:
    """The failure this guards: `purge_derived("../../blobs")` deleting every
    original in the archive and returning True to say it worked."""
    with pytest.raises(ValueError):
        artifacts.purge_derived("../../blobs")
