#!/usr/bin/env python3
"""A stand-in for `docker`, so `scripts/restore-drill.sh` can be *run* by tests.

The restore drill is the deliverable of Phase 6 — "I restored to an empty
container and found the DD-214" — and until CR-041 the only thing testing it was
a grep for six substrings. Every one of those strings survives a script whose
control flow is broken: an `exit 1` inside a subshell, a loop over an empty
list, a missing `set -e`.

The suite runs inside a container with no Docker socket, so the real drill
cannot start a real Postgres here; CI runs that one, against the seeded corpus,
in the e2e job. What this makes testable is everything else, and it is the part
that rots: the argument guards, the manifest read, the blob and vault presence
loops, the hit count, the exit codes and the cleanup trap — all of it really
executed, by bash, against a real backup directory on disk.

Driven by the environment, so one script covers the passing and failing drills:

    FAKE_DOCKER_LOG           append every argv here, tab-separated
    FAKE_DOCKER_RESTORE_EXIT  what `pg_restore` should exit with (default 0)
    FAKE_DOCKER_SHAS          file whose contents the source-file query returns
    FAKE_DOCKER_VAULT         ditto, for the vault_item query
    FAKE_DOCKER_HITS          ditto, for the full-text search
    FAKE_DOCKER_FORMS         ditto, for the known-form search

An unset file means the query returns nothing, which is the honest default: a
query that matched no rows is exactly what a failing drill looks like.
"""

import os
import sys
from pathlib import Path


def _emit(variable: str) -> None:
    path = os.environ.get(variable)
    if path and Path(path).is_file():
        sys.stdout.write(Path(path).read_text())


def main() -> int:
    argv = sys.argv[1:]

    log = os.environ.get("FAKE_DOCKER_LOG")
    if log:
        with open(log, "a") as handle:
            handle.write("\t".join(argv).replace("\n", " ") + "\n")

    if not argv or argv[0] != "exec":
        # rm, run, cp, ps — nothing to say, and nothing to fail.
        return 0

    joined = " ".join(argv)

    if "pg_isready" in joined:
        return 0
    if "pg_restore" in joined:
        return int(os.environ.get("FAKE_DOCKER_RESTORE_EXIT", "0"))
    if "CREATE EXTENSION" in joined:
        return 0

    # Order matters: the search query joins `source_file` too, so the most
    # specific fragment of each statement has to be tested first.
    if "text_tsv" in joined:
        _emit("FAKE_DOCKER_HITS")
    elif "known_form" in joined:
        _emit("FAKE_DOCKER_FORMS")
    elif "vault_item" in joined:
        _emit("FAKE_DOCKER_VAULT")
    elif "FROM source_file" in joined:
        _emit("FAKE_DOCKER_SHAS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
