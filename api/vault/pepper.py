"""The host secret that makes a six-digit PIN defensible (T-16.2, REQ-178).

A PIN is at most a few million guesses. A lockout counter stops a person at the
keyboard and does nothing at all for someone holding a disk image, because they
are not asking the application anything.

So the PIN-wrapped data key is derived from the PIN *and* this file, which
never leaves the machine. The consequence is the point:

| Attacker has                          | PIN path usable? |
|---------------------------------------|------------------|
| The S3 copy, or a stolen backup drive  | No               |
| A database dump                        | No               |
| The whole host, powered off            | Yes, at Argon2id speed |

Which means this file must be excluded from the local backup, from `make
export`, and from offsite replication — and that exclusion is asserted by
`tests/test_vault_pepper.py` rather than remembered.
"""

import logging
import os
from pathlib import Path

from api.config import get_settings
from api.vault import crypto

log = logging.getLogger("bindery.vault")

# Under the data root, beside `vault/objects/` rather than inside it. That
# placement is what decides where it travels: the local backup copies this file
# on purpose (T-16.10), and offsite replication walks `vault/objects/` only, so
# the pepper never leaves the building. Widening the offsite walk to the parent
# directory would start shipping it — `tests/test_vault_offsite.py` asserts
# otherwise. Deliberately not in `blobs/`, and not somewhere `archive_export`
# walks.
PEPPER_NAME = "vault-pepper.key"


def pepper_path() -> Path:
    return get_settings().data_root / "vault" / PEPPER_NAME


def load_or_create() -> bytes:
    """Read the pepper, creating it on first use.

    Created on first use rather than at install time so a stack that never uses
    the vault never has one to lose — and so its absence is a meaningful signal
    rather than a step somebody skipped.
    """
    path = pepper_path()
    if path.exists():
        pepper = path.read_bytes()
        if len(pepper) < crypto.PEPPER_BYTES:
            # Refusing beats silently deriving from a truncated secret, which
            # would weaken every PIN in the archive with nothing to show for it.
            raise crypto.VaultError(
                f"{path} is truncated. Restore it from wherever you kept it — "
                "without it, PIN unlock is impossible and the passphrase is the "
                "only way in."
            )
        return pepper

    path.parent.mkdir(parents=True, exist_ok=True)
    pepper = crypto.new_pepper()
    # Written 0600 and never logged. `os.open` rather than `write_bytes` so the
    # mode is set at creation instead of after, which would leave a window.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(handle, pepper)
    finally:
        os.close(handle)
    log.warning(
        "created a new vault pepper at %s — local backups carry it, offsite "
        "copies deliberately do not. Losing it costs PIN unlock only; the vault "
        "passphrase still opens everything.", path,
    )
    return pepper


def exists() -> bool:
    return pepper_path().exists()
