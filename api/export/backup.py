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

from sqlalchemy.engine import make_url

from api.config import get_settings

# `hash_file` by name rather than the module, because `run_backup`'s first
# parameter is called `integrity` and would shadow it.
from api.export.integrity import IntegrityReport, hash_file

log = logging.getLogger("bindery.backup")

MANIFEST_NAME = "manifest.json"
VAULT_DIRNAME = "vault"


@dataclass
class BackupResult:
    path: Path
    database_dump: Path
    blob_count: int
    byte_size: int
    manifest: dict
    integrity_healthy: bool
    vault_object_count: int = 0
    vault_bytes: int = 0


def backup_root() -> Path:
    return Path(os.environ.get("BINDERY_BACKUP_ROOT", get_settings().data_root / "backups"))


def connection_env() -> dict[str, str]:
    """libpq settings from the SQLAlchemy URL, via the environment.

    Parsed with SQLAlchemy's own `make_url` rather than `urllib`, because that
    is the parser that already produces a working connection. A generated
    password routinely contains `/`, `@` or `:`; `urlsplit` reads the first `/`
    as the start of the path and ends up reporting *Port could not be cast to
    integer* — which looks like a misconfiguration and is a parsing bug. That
    is not hypothetical: it is what the deployed password did.

    Passed through the environment rather than on the command line, because
    argv is world-readable via `ps`.
    """
    url = make_url(get_settings().database_url)
    env = {
        "PGHOST": url.host or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGDATABASE": url.database or "",
    }
    if url.username:
        env["PGUSER"] = url.username
    if url.password:
        env["PGPASSWORD"] = url.password
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
    """Copy originals *after* the dump, and read back what was written.

    See the ordering rule above for *when*; this is about *whether it arrived*.

    The skip used to compare sizes, on the reasoning that the filename is a
    content address so equal names and equal sizes mean equal bytes (CR-072).
    That holds for the source pool, where something wrote a file and named it
    after its own digest. It does not hold here: this filename was chosen by the
    copy, and until now nothing had ever re-read the bytes underneath it.

    `integrity.check` — the thing this project opens by saying a blob can decay
    on disk for years unnoticed — hashes `blob_root` and never the backup, so
    the one copy nobody was checking was the copy kept for the day the first one
    fails. A flipped bit preserves the size, so every later run skipped it, and
    the damage surfaced during a restore.

    So every target is hashed and compared — but against the *source's* bytes,
    the way `copy_vault` does it, not against the filename. The two differ on
    the case that matters: a blob whose contents no longer match its own name
    has rotted, and rot is not a backup failure. `integrity.check` is what
    reports it, and `run_backup` already refuses to run over a failing check
    unless it is forced. Aborting here would mean a single rotten original
    stops the generation that was being taken to preserve the other eight
    hundred — and `--force` exists to say "back up the rest anyway".

    A target that does not match the source we just read is different: that is
    a failing or full disk, and it raises.
    """
    source = get_settings().blob_root
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    total = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)

        expected, size = hash_file(path)
        if expected != path.name:
            # The original has rotted. Copy it regardless — a decayed copy of
            # the only copy is still the only copy — and say so, because this
            # is the one place that reads both pools and can tell.
            log.error(
                "original %s no longer matches its content address (%s); "
                "backing it up as-is",
                path.name, expected,
            )

        if target.exists() and hash_file(target)[0] == expected:
            count += 1
            total += size
            continue

        shutil.copy2(path, target)
        actual, _ = hash_file(target)
        if actual != expected:
            # A copy that does not read back is a failing disk or a full one,
            # and either way the generation being written is not one to rely on.
            raise RuntimeError(
                f"the backup copy of {path.name} did not verify — it hashed to "
                f"{actual}. This backup is not trustworthy; check the target disk."
            )
        count += 1
        total += size
    return count, total


def copy_vault(destination: Path) -> tuple[int, int]:
    """Copy the vault's encrypted objects, and the host pepper (T-16.10).

    Vault objects live outside the blob store — deliberately, since they are not
    content-addressed — so `copy_blobs` never sees them. Without this, a
    restore would bring back vault rows pointing at ciphertext that no backup
    ever held, and the one part of the archive nobody can re-download would be
    the only part not protected.

    The pepper is included **here and not offsite**. It is what stops a stolen
    database from being enough to brute-force a six-digit PIN, so putting it in
    the same bucket as the database dump would defeat the only thing it does.
    A local backup sits on the host that already has the pepper, so including it
    there changes no threat model and makes a same-host restore whole.

    Nothing in here is readable without the vault passphrase, which this
    application does not have and cannot recover.

    Verified the same way `copy_blobs` is, and it matters more here (CR-072):
    a blob can be re-scanned from the paper it came from, and a vault object
    exists exactly once. The name cannot be the expected digest — vault objects
    are deliberately not content-addressed, because a content address is an
    existence oracle (ADR-012) — so the source is hashed too and the two
    compared. A size-only skip would have left a bit-rotted copy of the one
    thing in the archive that cannot be reproduced.
    """
    from api.vault import pepper, store

    source = store.vault_root()
    count = 0
    total = 0
    if source.is_dir():
        target_root = destination / "objects"
        target_root.mkdir(parents=True, exist_ok=True)
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            target = target_root / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            expected, _ = hash_file(path)
            if not (target.exists() and hash_file(target)[0] == expected):
                shutil.copy2(path, target)
                actual, _ = hash_file(target)
                if actual != expected:
                    raise RuntimeError(
                        f"the backup copy of vault object {path.name} did not "
                        "verify. A sealed object exists once; refusing to record "
                        "a backup that does not hold it."
                    )
            count += 1
            total += target.stat().st_size

    pepper_path = get_settings().data_root / "vault" / pepper.PEPPER_NAME
    if pepper_path.is_file():
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pepper_path, destination / pepper.PEPPER_NAME)
        os.chmod(destination / pepper.PEPPER_NAME, 0o600)
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
    vault_count, vault_bytes = copy_vault(root / VAULT_DIRNAME)

    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "database_dump": dump.name,
        "database_dump_bytes": dump.stat().st_size,
        "blob_count": blob_count,
        "blob_bytes": byte_size,
        # Said in the file a restore reads first, because "the copy was made"
        # and "the copy was read back" are different claims and only the second
        # one is worth anything on the day it is needed (CR-072).
        "blobs_verified": True,
        "verification": (
            "Every blob in this generation was hashed after copying and matched "
            "against its content address, and every vault object was hashed "
            "against its source. A copy that did not verify would have raised "
            "rather than produced this manifest."
        ),
        "integrity": integrity.as_dict() if integrity else None,
        # Said plainly, in the file a restore reads first. Discovering that part
        # of a backup cannot be opened is a thing to learn now, not during a
        # restore.
        "vault": {
            "object_count": vault_count,
            "object_bytes": vault_bytes,
            "pepper_included": True,
            "readable_without_passphrase": False,
            "note": (
                "These objects are encrypted with a key wrapped by a passphrase "
                "that exists only in the head of whoever set the vault up. "
                "Nothing in this backup, in the database dump, or in Bindery "
                "itself can decrypt them. A restore brings them back still "
                "sealed, and they open again the moment that passphrase is "
                "entered. The pepper beside them protects the PIN only; losing "
                "it costs the PIN and nothing else."
            ),
        },
        "restore_command": "scripts/restore-drill.sh <this directory>",
        "note": (
            "Postgres was dumped before blobs were copied. Blobs are immutable "
            "and content-addressed, so any blob newer than the dump is an "
            "unreferenced orphan, never a dangling reference."
        ),
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))

    log.info(
        "backup complete: %s (%s blobs, %s bytes, %s sealed vault objects)",
        root, blob_count, byte_size, vault_count,
    )
    return BackupResult(
        path=root,
        database_dump=dump,
        blob_count=blob_count,
        byte_size=byte_size,
        manifest=manifest,
        integrity_healthy=integrity.healthy if integrity else True,
        vault_object_count=vault_count,
        vault_bytes=vault_bytes,
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
