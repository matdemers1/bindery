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


# The single documented exception (ADR-012, T-16.5). Moving a document into the
# vault deletes its plaintext — that is what the feature *is*, and writing an
# encrypted copy beside the original would be theatre.
#
# It is permitted because it is neither automatic nor unattended: a person
# selected a document and asked for exactly this. REQ-090 exists to stop the
# archive discarding things on its own — a scheduled prune, a cleanup job, an
# eager cascade — and none of those are happening here.
#
# Named as one file rather than a loosened pattern, so the exemption cannot
# spread by accident. `test_the_vault_exception_stays_one_file` below is what
# keeps that true.
VAULT_EXCEPTION = "api/vault/store.py"


def test_no_destructive_database_path() -> None:
    offences: list[str] = []
    for package in SEARCHED:
        for path in sorted((ROOT / package).rglob("*.py")):
            if str(path.relative_to(ROOT)) == VAULT_EXCEPTION:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                for pattern in FORBIDDEN:
                    if pattern.search(line):
                        relative = path.relative_to(ROOT)
                        offences.append(f"{relative}:{lineno}: {line.strip()}")

    assert not offences, "destructive database path found (REQ-090):\n" + "\n".join(offences)


def test_the_vault_exception_stays_one_file() -> None:
    """The exemption is a door, and doors get propped open.

    Two things are asserted: the exempt file still exists (so the exemption is
    not silently covering nothing), and no *other* file has quietly started
    deleting rows by being added to it. The second is the one that matters —
    the way REQ-090 erodes is not by someone arguing against it, it is by an
    exception list growing one entry at a time.
    """
    exempt = ROOT / VAULT_EXCEPTION
    assert exempt.is_file(), f"{VAULT_EXCEPTION} is gone; drop the exception with it"

    source = exempt.read_text()
    assert "ADR-012" in source, "the exception must point at the decision that granted it"
    # And it deletes exactly what the ADR says it deletes: pages, the vault
    # rows that replace them, and nothing that is an original record.
    for allowed in ("session.delete(page)", "session.delete(item)"):
        assert allowed in source
    assert "DELETE FROM" not in source.upper(), "raw SQL deletion is not what was granted"


def test_the_vault_verifies_before_it_destroys() -> None:
    """The ordering that separates a vault from a shredder.

    Encrypt, read the file back, compare against the source hash, and only then
    unlink. Asserted structurally because the alternative — a test that deletes
    a real document to prove the order — is a test that can lose one.
    """
    source = (ROOT / VAULT_EXCEPTION).read_text()
    verify = source.index("if not verified:")
    unlink = source.index("plaintext_path.unlink")
    assert verify < unlink, (
        "the plaintext is being deleted before the ciphertext is verified against "
        "the original hash"
    )
    # The verify has to fail closed. A bare `except` around the read-back that
    # left `verified` alone, or a hash comparison that never ran because the
    # decrypt raised first, would both keep the ordering above and still delete
    # a document that could not be recovered.
    assert "verified = False" in source, (
        "the read-back no longer has a failure branch that refuses the move"
    )
    assert source.index("except crypto.WrongSecret") < unlink, (
        "a ciphertext that fails to decrypt on read-back must refuse the move, "
        "not escape past the delete as an unhandled error"
    )
