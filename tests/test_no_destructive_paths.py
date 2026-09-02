"""REQ-090 — nothing is ever automatically deleted.

A negative requirement cannot be demonstrated by a feature, so it is enforced
here: no destructive database path may exist in the application or the worker
outside the one the vault was granted. Migrations are exempt (they are applied
explicitly by a human) and so are the tests themselves.

Two things are guarded, because the requirement has two halves. *What* deletes
is the pattern search below, exempting one file. *Who can reach it* is
`SEALING_CALLERS` — the half that was missing, and the half that moved when
Phase 18 put the vault seal on a fifteen-second timer without touching the
exemption list at all.

If this fails, the fix is to revoke, tombstone, or supersede — not to loosen the
pattern list.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEARCHED = ("api", "worker")

# The first two are the exact patterns the Phase 0 plan greps for; the rest close
# the obvious ways to spell the same thing in SQLAlchemy.
#
# `\.delete\(` rather than `\.delete\(\)`: the original required literal empty
# parentheses, so `query.delete(synchronize_session=False)` — the spelling the
# ORM documentation actually gives for a bulk delete — walked straight past it.
# The cost of widening it is `@router.delete("/…")`, which is a route and not a
# deletion; decorator lines are skipped below rather than carved out by name.
FORBIDDEN = (
    re.compile(r"DELETE FROM", re.IGNORECASE),
    re.compile(r"\.delete\("),
    # `delete(Model)` — the bare constructor, reached by `from sqlalchemy import
    # delete`, which no `sa.`-prefixed pattern can see.
    re.compile(r"(?<![.\w])delete\(\s*[A-Z]"),
    re.compile(r"^\s*from sqlalchemy import .*\bdelete\b"),
    re.compile(r"\bDROP TABLE\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
)

# Scope, stated so the green is read for what it is: this guard is about
# *database* deletion. Filesystem deletion — `unlink`, `rmtree` — is not
# searched here, and the vault's own unlink ordering is asserted separately by
# `test_the_vault_verifies_before_it_destroys`.


# The single documented exception (ADR-012, T-16.5). Moving a document into the
# vault deletes its plaintext — that is what the feature *is*, and writing an
# encrypted copy beside the original would be theatre.
#
# What permits it is a person's decision, and since Phase 18 that decision is
# no longer always per-document. Two things reach it, and both are named in
# `SEALING_CALLERS` below:
#
#   - a person opened one document and pressed "move into the vault";
#   - a person ticked "into the vault" for an import and unlocked their vault,
#     after which the sweep seals each file as the pipeline lets go of it
#     (REQ-197). That runs on a fifteen-second timer, so the *seal* is
#     unattended even though the *decision* was not, and the audit row says so:
#     `actor_type=SYSTEM`, `actor_id=<the person who asked>`.
#
# It is written down that way because the earlier comment here claimed the seal
# was "neither automatic nor unattended", which stopped being true the day the
# sweep shipped — and a guard that asserts something false about the code it
# guards is worse than no guard, because the green gets read as evidence.
#
# What REQ-090 still forbids absolutely is the archive discarding things on its
# own initiative: a scheduled prune, a cleanup job, an eager cascade. The sweep
# is none of those — it seals only files whose import a person bound to the
# vault, only while that person's vault is open, and it never decides on its own
# that something should go.
#
# Named as one file rather than a loosened pattern, so the exemption cannot
# spread by accident. `test_the_vault_exception_stays_one_file` below is what
# keeps that true, and `test_the_vault_is_reached_only_from_declared_callers`
# is what keeps the *other* way it erodes closed: the list never grew, a caller
# was added instead.
VAULT_EXCEPTION = "api/vault/store.py"

# Every file that may destroy a plaintext original by calling into the exempt
# module, and the human decision that stands behind it.
SEALING_CALLERS = {
    "api/routers/vault.py": (
        "a person opened one document and asked for it — move in, move out, and "
        "the re-seal that upgrades an already-vaulted object's format"
    ),
    "api/vault/sweep.py": (
        "the vault sweep (REQ-197): a person bound an import to the vault and "
        "unlocked it, and each file is sealed as the pipeline finishes with it"
    ),
}

# `\w*store\.` rather than `store\.`, because `from api.vault import store as
# vault_store` is already how one module imports it.
_SEALS = re.compile(r"\b\w*store\.(?:seal|unseal|reseal)\(")
_IMPORTS_SEALING = re.compile(r"^\s*from api\.vault\.store import .*\b(?:un|re)?seal\b")


def test_no_destructive_database_path() -> None:
    offences: list[str] = []
    for package in SEARCHED:
        for path in sorted((ROOT / package).rglob("*.py")):
            if str(path.relative_to(ROOT)) == VAULT_EXCEPTION:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if line.lstrip().startswith("@"):
                    continue  # `@router.delete("/…")` is a route, not a deletion
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


def test_the_vault_is_reached_only_from_declared_callers() -> None:
    """The exemption is granted by *path*, which is the wrong shape for it.

    A path exemption answers "which file may delete?" when the question REQ-090
    actually asks is "who can reach the deleting code, and who asked?". The
    difference is not academic: the exception list never grew by an entry, and
    plaintext destruction still became a background task, because Phase 18 added
    a *caller* — and a guard that only watches the list saw nothing.

    So the callers are enumerated too, each with the decision that justifies it.
    A new one has to be argued for here before it can ship, which is the point.
    """
    found: dict[str, list[int]] = {}
    for package in SEARCHED:
        for path in sorted((ROOT / package).rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            if relative == VAULT_EXCEPTION:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if _SEALS.search(line) or _IMPORTS_SEALING.search(line):
                    found.setdefault(relative, []).append(lineno)

    undeclared = {name: lines for name, lines in found.items() if name not in SEALING_CALLERS}
    assert not undeclared, (
        "a new caller can destroy a plaintext original and is not declared "
        f"(REQ-090, ADR-012): {undeclared}. Add it to SEALING_CALLERS with the "
        "human decision that stands behind it, or route it through one that is "
        "already there."
    )

    unused = set(SEALING_CALLERS) - set(found)
    assert not unused, f"declared as a caller but no longer calls anything: {sorted(unused)}"


def test_the_sweep_seals_only_what_a_person_asked_for() -> None:
    """The unattended caller, pinned to the three things that keep it honest.

    Structural, because the behaviour itself is covered by
    `tests/test_import_to_vault.py` and what is being defended here is the
    justification written above: the sweep may seal only files whose import a
    person bound to the vault, only while that person's vault is open, and it
    must record the person it acted for.
    """
    source = (ROOT / "api/vault/sweep.py").read_text()
    assert "ImportSession.to_vault.is_(True)" in source, (
        "the sweep no longer restricts itself to imports a person bound to the vault"
    )
    assert "sessions.peek(" in source, (
        "the sweep must read the unlock without extending it — see ADR-012's idle "
        "timeout, which its own polling would otherwise hold open forever"
    )
    assert "actor_id=owner" in source, (
        "the audit row must name the person whose decision this seal rests on"
    )
