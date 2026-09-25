"""The pre-deploy database dump Shipyard runs before a swap (BND-T-002, SHP-D-018/054).

Not the nightly backup. That one runs the integrity check and copies every blob, which is right
at 03:30 and wrong in the middle of a deploy. Before a migration only the database can change in a
way that needs undoing: blobs are immutable and content-addressed, so a blob written after this
dump is a harmless orphan to a restore, never a contradiction.

Shipyard's backup step looks for exactly one new non-empty file directly under its artifacts
directory, so the dump lands as a single file, written under a `.partial` name and renamed only
once pg_dump has succeeded — a half-written dump is never mistaken for an artifact.

    python -m api.export.cli dump
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from api.export.backup import backup_root, dump_database

KEEP = 10
PREFIX = "bindery-"
SUFFIX = ".dump"


def predeploy_root() -> Path:
    return backup_root() / "predeploy"


def run_predeploy_dump(
    root: Path | None = None,
    *,
    dump: Callable[[Path], Path] = dump_database,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    keep: int = KEEP,
) -> Path:
    """Dump the database to `<root>/bindery-<UTC stamp>.dump` and return that path."""
    root = root or predeploy_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%dT%H%M%S%fZ")
    final = root / f"{PREFIX}{stamp}{SUFFIX}"
    partial = root / f"{PREFIX}{stamp}{SUFFIX}.partial"
    try:
        dump(partial)
        if not partial.exists() or partial.stat().st_size == 0:
            raise RuntimeError("pg_dump reported success but wrote nothing")
        partial.rename(final)
    finally:
        partial.unlink(missing_ok=True)
    _prune(root, keep)
    return final


def _prune(root: Path, keep: int) -> None:
    dumps = sorted(
        p
        for p in root.iterdir()
        if p.is_file() and p.name.startswith(PREFIX) and p.name.endswith(SUFFIX)
    )
    for old in dumps[: max(0, len(dumps) - keep)]:
        old.unlink(missing_ok=True)
