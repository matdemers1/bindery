"""REQ-090 — nothing is ever automatically deleted.

A negative requirement cannot be demonstrated by a feature, so it is enforced
here: no unattended destructive database path may exist in the application or
the worker. Migrations are exempt (they are applied explicitly by a human) and
so are the tests themselves.

If this fails, the fix is to revoke, tombstone, or supersede — not to loosen the
pattern list.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEARCHED = ("api", "worker")

# The first two are the exact patterns the Phase 0 plan greps for; the rest close
# the obvious ways to spell the same thing in SQLAlchemy.
FORBIDDEN = (
    re.compile(r"DELETE FROM", re.IGNORECASE),
    re.compile(r"\.delete\(\)"),
    re.compile(r"\bsession\.delete\("),
    re.compile(r"\bsa\.delete\("),
    re.compile(r"\bDROP TABLE\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
)


def test_no_destructive_database_path() -> None:
    offences: list[str] = []
    for package in SEARCHED:
        for path in sorted((ROOT / package).rglob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                for pattern in FORBIDDEN:
                    if pattern.search(line):
                        relative = path.relative_to(ROOT)
                        offences.append(f"{relative}:{lineno}: {line.strip()}")

    assert not offences, "destructive database path found (REQ-090):\n" + "\n".join(offences)
