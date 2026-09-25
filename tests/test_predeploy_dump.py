"""The pre-deploy dump Shipyard's backup step runs (BND-T-002)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.export import predeploy


def _fake_dump(content: bytes = b"PGDMP"):
    calls: list[Path] = []

    def dump(destination: Path) -> Path:
        calls.append(destination)
        destination.write_bytes(content)
        return destination

    return dump, calls


def _clock(start: datetime):
    t = [start]

    def now() -> datetime:
        t[0] = t[0] + timedelta(seconds=1)
        return t[0]

    return now


def test_writes_one_file_via_a_partial_name(tmp_path):
    dump, calls = _fake_dump()
    now = _clock(datetime(2026, 9, 25, tzinfo=UTC))
    path = predeploy.run_predeploy_dump(tmp_path, dump=dump, now=now)
    assert calls == [path.with_name(path.name + ".partial")]
    assert path.read_bytes() == b"PGDMP"
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
    assert path.name.startswith("bindery-") and path.name.endswith(".dump")


def test_an_empty_dump_is_refused_and_leaves_nothing(tmp_path):
    dump, _ = _fake_dump(b"")
    with pytest.raises(RuntimeError, match="wrote nothing"):
        predeploy.run_predeploy_dump(tmp_path, dump=dump)
    assert list(tmp_path.iterdir()) == []


def test_a_failed_dump_leaves_no_partial(tmp_path):
    def dump(destination: Path) -> Path:
        destination.write_bytes(b"half")
        raise RuntimeError("pg_dump failed: boom")

    with pytest.raises(RuntimeError, match="boom"):
        predeploy.run_predeploy_dump(tmp_path, dump=dump)
    assert list(tmp_path.iterdir()) == []


def test_keeps_the_newest_ten(tmp_path):
    dump, _ = _fake_dump()
    now = _clock(datetime(2026, 9, 25, tzinfo=UTC))
    made = [predeploy.run_predeploy_dump(tmp_path, dump=dump, now=now) for _ in range(12)]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == sorted(p.name for p in made[-10:])
