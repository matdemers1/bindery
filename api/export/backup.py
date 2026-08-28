"""3-2-1 backup, and the manifest that makes a restore verifiable (T-6.6, REQ-096).

Three copies, two media, one offsite. Here that means: the live RAID-5 pool, a
local backup target, and an encrypted offsite copy.

Two ordering rules, and both are load-bearing:

1. **Integrity check before backup.** A backup taken over a corrupt blob is a
   corrupt backup, faithfully replicated and eventually rotated into every
   generation you have.
2. **Postgres before blobs.** Blobs are content-addressed, immutable and
   append-only, so a blob written after the database dump is simply not
   referenced by it — a harmless orphan. Do it the other way round and the dump
   can reference a blob the backup does not contain, which is a dangling
   reference and an unrestorable archive. The safe order is safe *by
   construction*, not by locking.

The offsite copy is encrypted because it is, by definition, somewhere you do not
control.
"""

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from api.config import get_settings
from api.export.integrity import IntegrityReport

log = logging.getLogger("bindery.backup")

MANIFEST_NAME = "manifest.json"


@dataclass
class BackupResult:
    path: Path
    database_dump: Path
    blob_count: int
    byte_size: int
    manifest: dict
    integrity_healthy: bool


def backup_root() -> Path:
    return Path(os.environ.get("BINDERY_BACKUP_ROOT", get_settings().data_root / "backups"))


def connection_env() -> dict[str, str]:
    """libpq settings from the SQLAlchemy URL, via the environment.

    Not a connection string on the command line, for two reasons. A password
    containing `@` or `:` makes a URL ambiguous — the real one here parsed as a
    port and produced *invalid integer value ... for connection option "port"*,
    which reads like a config error and is actually a quoting bug. And argv is
    world-readable through `ps`, so a password on the command line is a password
    anyone with a shell can see.
    """
    parsed = urlsplit(get_settings().database_url.replace("+asyncpg", ""))
    env = {
        "PGHOST": parsed.hostname or "localhost",
        "PGPORT": str(parsed.port or 5432),
        "PGDATABASE": (parsed.path or "/").lstrip("/"),
    }
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    return env


def dump_database(destination: Path) -> Path:
    """`pg_dump -Fc`, the custom format, because it restores selectively."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("pg_dump") is None:
        raise RuntimeError(
            "pg_dump is not installed in this image, so no backup can be taken. "
            "This is a deployment problem, not a data problem."
        )
    try:
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", f"--file={destination}"],
            check=True,
            capture_output=True,
            env={**os.environ, **connection_env()},
        )
    except subprocess.CalledProcessError as error:
        # The reason matters — a version mismatch and a bad password look
        # identical from the exit code alone.
        detail = error.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"pg_dump failed: {detail}") from error
    return destination


def copy_blobs(destination: Path) -> tuple[int, int]:
    """Copy originals *after* the dump. See the ordering rule above."""
    source = get_settings().blob_root
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    total = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == path.stat().st_size:
            # Content-addressed: same name and same size means same bytes, so a
            # nightly backup only ever copies what is new.
            count += 1
            total += target.stat().st_size
            continue
        shutil.copy2(path, target)
        count += 1
        total += target.stat().st_size
    return count, total


def run_backup(
    integrity: IntegrityReport | None = None,
    *,
    destination: Path | None = None,
    allow_unhealthy: bool = False,
) -> BackupResult:
    """One backup generation, with a manifest a restore can be checked against."""
    if integrity is not None and not integrity.healthy and not allow_unhealthy:
        # Refusing is the right default. A backup of known-corrupt data is worse
        # than no new backup, because it eventually rotates out the good one.
        raise RuntimeError(
            f"integrity check failed ({len(integrity.corrupt)} corrupt, "
            f"{len(integrity.missing)} missing) — refusing to back up over it"
        )

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    root = destination or (backup_root() / stamp)
    root.mkdir(parents=True, exist_ok=True)

    dump = dump_database(root / "bindery.dump")
    blob_count, byte_size = copy_blobs(root / "blobs")

    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "database_dump": dump.name,
        "database_dump_bytes": dump.stat().st_size,
        "blob_count": blob_count,
        "blob_bytes": byte_size,
        "integrity": integrity.as_dict() if integrity else None,
        "restore_command": "scripts/restore-drill.sh <this directory>",
        "note": (
            "Postgres was dumped before blobs were copied. Blobs are immutable "
            "and content-addressed, so any blob newer than the dump is an "
            "unreferenced orphan, never a dangling reference."
        ),
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))

    log.info("backup complete: %s (%s blobs, %s bytes)", root, blob_count, byte_size)
    return BackupResult(
        path=root,
        database_dump=dump,
        blob_count=blob_count,
        byte_size=byte_size,
        manifest=manifest,
        integrity_healthy=integrity.healthy if integrity else True,
    )


def encrypt_for_offsite(source: Path, destination: Path, passphrase: str) -> Path:
    """Encrypt a backup generation before it leaves the building.

    age would be the better tool; openssl is here because it is already on every
    host that runs Postgres, and an offsite copy you can decrypt with a binary
    you already have is worth more than one that needs a download first.
    """
    if len(passphrase or "") < 16:
        raise ValueError("an offsite passphrase must be at least 16 characters")
    destination.parent.mkdir(parents=True, exist_ok=True)

    tarball = destination.with_suffix(".tar")
    subprocess.run(
        ["tar", "-cf", str(tarball), "-C", str(source.parent), source.name],
        check=True, capture_output=True,
    )
    try:
        subprocess.run(
            [
                "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
                "-salt", "-in", str(tarball), "-out", str(destination),
                "-pass", "env:BINDERY_OFFSITE_PASSPHRASE",
            ],
            check=True, capture_output=True,
            # Via the environment, never argv: argv is world-readable in `ps`.
            env={**os.environ, "BINDERY_OFFSITE_PASSPHRASE": passphrase},
        )
    finally:
        tarball.unlink(missing_ok=True)

    log.info("offsite copy encrypted to %s", destination)
    return destination


def verify_encrypted(path: Path) -> bool:
    """Confirm an offsite file is actually ciphertext before shipping it.

    Cheap, and it catches the failure that matters: an encryption step that
    silently no-opped and left your medical records in plaintext on someone
    else's disk.
    """
    if not path.is_file() or path.stat().st_size < 32:
        return False
    with path.open("rb") as handle:
        header = handle.read(16)
    # openssl's PBKDF2 container starts with the literal "Salted__" magic.
    return header.startswith(b"Salted__")
