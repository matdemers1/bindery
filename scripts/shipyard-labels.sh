#!/usr/bin/env bash
# The two labels Shipyard reads off every published image (SHP-D-019/021/022,
# BND-T-001) — computed here, once, so CI's `images` job and a developer
# checking a release by hand agree on the answer instead of each carrying
# their own copy of the logic.
#
#   dev.d3cloud.shipyard.migration  the release's migration posture
#   dev.d3cloud.shipyard.schema     the alembic head this commit expects the
#                                    database to reach
#
# Usage:
#   scripts/shipyard-labels.sh                # both labels, KEY=VALUE per line
#   scripts/shipyard-labels.sh migration       # migration only
#   scripts/shipyard-labels.sh schema          # schema only
#
# Migrations are applied explicitly, never on container boot (invariant 10),
# and Bindery never migrates from inside a running image — Shipyard runs
# `alembic upgrade head` as a one-shot of the *new* image before swapping
# (SHP-D-021/028). `dev.d3cloud.shipyard.migration=contract` marks a release
# that dropped or renamed something a previous release still reads, so
# Shipyard must never auto-roll a `contract` release back to the image before
# it. `expand` and `none` may be rolled back freely; `none` is the default for
# a commit with no trailer, which is the ordinary case (an ordinary column
# addition, or no migration at all).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

migration() {
    local trailer
    trailer=$(git log -1 --format='%(trailers:key=Shipyard-Migration,valueonly)' | tr -d '[:space:]')
    case "$trailer" in
        "") echo "none" ;;
        expand|contract|none) echo "$trailer" ;;
        *)
            echo "shipyard-labels: bogus Shipyard-Migration trailer '$trailer' (want expand|contract|none, or omit it)" >&2
            exit 1
            ;;
    esac
}

# The newest revision in alembic/versions, found by walking down_revision —
# the same approach as `api/version._head_revision`, duplicated rather than
# imported because this runs before any Python dependency is installed and
# must work from nothing but the checkout. Kept a small, separately-readable
# copy on purpose: a shell one-liner here is easier to audit in a CI log than
# a call into the application.
schema() {
    local dir="alembic/versions"
    if [ ! -d "$dir" ]; then
        echo "shipyard-labels: $dir not found" >&2
        exit 1
    fi

    # One python3 call over the whole directory rather than one per file per
    # field: a heredoc nested inside `$( … )` command substitution trips a
    # bash quote-parsing bug on a pattern containing `["\']` (reproduced with
    # bash 3.2 and 5.x both) — the file is the workaround, not a style choice.
    local out
    out="$(mktemp)"
    trap 'rm -f "$out"' RETURN
    python3 - "$dir" > "$out" <<'PY'
# Same approach as api/version.py's _head_revision: the newest revision is the
# one nothing else points `down_revision` at. Kept as its own small copy
# rather than an import — this has to run before any Python dependency is
# installed, from nothing but the checkout.
import re
import sys
from pathlib import Path

ASSIGNMENT = re.compile(
    r"^(?P<name>\w+)\s*(?::[^=\n]*)?=\s*(?P<quote>[\"'])(?P<value>[^\"']*)(?P=quote)",
    re.M,
)


def assigned(text: str, name: str) -> str | None:
    for match in ASSIGNMENT.finditer(text):
        if match.group("name") == name:
            return match.group("value")
    return None


directory = Path(sys.argv[1])
revisions: dict[str, str | None] = {}
for file in directory.glob("*.py"):
    text = file.read_text()
    revision = assigned(text, "revision")
    down = assigned(text, "down_revision")
    if revision:
        revisions[revision] = down

if not revisions:
    print("NONE", file=sys.stderr)
    sys.exit(1)

pointed_at = {down for down in revisions.values() if down}
heads = [rev for rev in revisions if rev not in pointed_at]
if len(heads) != 1:
    print(f"MULTIPLE {heads}", file=sys.stderr)
    sys.exit(1)
print(heads[0])
PY
    local status=$?
    if [ "$status" -ne 0 ]; then
        echo "shipyard-labels: could not find exactly one alembic head under $dir" >&2
        rm -f "$out"
        exit 1
    fi
    cat "$out"
    rm -f "$out"
}

case "${1-}" in
    migration) migration ;;
    schema) schema ;;
    "")
        echo "migration=$(migration)"
        echo "schema=$(schema)"
        ;;
    *)
        echo "usage: $0 [migration|schema]" >&2
        exit 2
        ;;
esac
