"""REQ-090 — nothing is ever automatically deleted.

A negative requirement cannot be demonstrated by a feature, so it is enforced
here: no destructive database path may exist in the application or the worker
outside the one the vault was granted. Migrations are exempt (they are applied
explicitly by a human) and so are the tests themselves.

Four things are guarded, because the requirement has more halves than it looks.

- *What deletes rows* is the pattern search below, exempting one file.
- *Who can reach it* is `SEALING_CALLERS` — the half that was missing, and the
  half that moved when Phase 18 put the vault seal on a fifteen-second timer
  without touching the exemption list at all.
- *What deletes files* is `FILESYSTEM_DELETION` (CR-125). `DELETE FROM` is not
  how an original is destroyed on this system; `shutil.rmtree` and
  `path.unlink()` are, and seven such sites existed outside the vault exemption
  while this guard could see none of them.
- *What deletes without being asked* is `ORM_CASCADES` — an eager
  `cascade="all, delete-orphan"` or `ondelete="CASCADE"`, which the module
  docstring has always named as a threat and which no pattern here could match.

If this fails, the fix is to revoke, tombstone, or supersede — not to loosen the
pattern list.
"""

import ast
import re
import textwrap
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

# The other two spellings of the same requirement (CR-125).
#
# The docstring above names the threats REQ-090 exists to stop — "a scheduled
# prune, a cleanup job, an eager cascade" — and until this section existed the
# patterns above could see none of them. `DELETE FROM` is not how an original
# gets destroyed on this system: `shutil.rmtree` is, and `path.unlink()` is, and
# a `cascade="all, delete-orphan"` added to a relationship in passing is the
# quietest of the three, because it deletes rows nobody wrote a delete for.
#
# Seven filesystem-deletion sites already existed outside the vault exemption
# when this was written. Each was argued safe in a docstring; none was asserted
# safe, and nothing stopped the eighth. They are named below by
# `path::function`, not by file, so a *new* deleting function in an
# already-listed module is still caught — the shape `test_boundary_guard.py`
# settled on for the same reason.
_FS_FUNCTIONS = {"unlink", "rmtree", "remove", "rmdir", "removedirs", "rmtree_errors"}
_CASCADE_KEYWORDS = {"cascade", "ondelete"}

FILESYSTEM_DELETION = {
    "api/artifacts.py::purge_derived": (
        "removes `derived/<sha256>/` when a document is sealed. Safe because "
        "nothing under `derived/` is an original — every byte is reproducible "
        "from the blob by re-running the pipeline — and leaving plaintext page "
        "renders beside an encrypted original would not be hiding it"
    ),
    "api/storage/blobs.py::store_stream": (
        "unlinks its own temp file: the upload it just wrote, when the content "
        "address already exists or when the write failed. It never touches a "
        "stored blob"
    ),
    "api/export/mirror.py::_link_or_copy": (
        "replaces one mirror entry, which is a hardlink to an immutable blob"
    ),
    "api/export/mirror.py::_prune": (
        "the mirror is a derived index regenerated from the database on every "
        "rebuild; every entry is a hardlink to a 0444 blob that is not touched. "
        "REQ-090 is about originals and records, not about tidying an index — "
        "and `test_the_mirror_prune_stays_inside_the_tree_it_was_given` below "
        "asserts it cannot reach outside the root it was handed"
    ),
    "api/export/backup.py::encrypt_for_offsite": (
        "removes the intermediate tarball it created a moment earlier, after "
        "the ciphertext beside it has been written"
    ),
}

# Nothing in the tree declares an ORM cascade today, and that is the point: the
# first one to appear should be argued for rather than noticed later. An eager
# cascade is the deletion nobody wrote — a `document` row going away taking its
# pages, its tags, its classifications and its audit history with it.
ORM_CASCADES: dict[str, str] = {}


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


# ---------------------------------------------------------------------------
# Filesystem deletion and ORM cascades (CR-125)
# ---------------------------------------------------------------------------


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, ast.AST | None]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    resolved: dict[ast.AST, ast.AST | None] = {}
    for node in ast.walk(tree):
        walker: ast.AST | None = node
        while walker is not None and not isinstance(
            walker, ast.FunctionDef | ast.AsyncFunctionDef
        ):
            walker = parents.get(walker)
        resolved[node] = walker
    return resolved


def _destructive_sites(source: str, where: str) -> tuple[dict[str, str], dict[str, str]]:
    """`(filesystem deletions, ORM cascades)` in one module, keyed `path::function`."""
    tree = ast.parse(source)
    enclosing = _enclosing_functions(tree)

    def key(node: ast.AST) -> str:
        function = enclosing.get(node)
        return f"{where}::{function.name if function else '<module>'}"

    filesystem: dict[str, str] = {}
    cascades: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
            )
            if name in _FS_FUNCTIONS:
                filesystem.setdefault(key(node), f"calls {name}()")
            for keyword in node.keywords:
                if keyword.arg in _CASCADE_KEYWORDS:
                    value = ast.unparse(keyword.value)
                    # `ondelete="RESTRICT"` / `"SET NULL"` refuse or blank a
                    # reference; they do not remove a record.
                    if "DELETE" in value.upper() or "delete" in value:
                        cascades.setdefault(key(node), f"{keyword.arg}={value}")
    return filesystem, cascades


def _scan_destructive() -> tuple[dict[str, str], dict[str, str], int]:
    filesystem: dict[str, str] = {}
    cascades: dict[str, str] = {}
    read = 0
    for package in SEARCHED:
        for path in sorted((ROOT / package).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative == VAULT_EXCEPTION:
                continue
            read += 1
            found_fs, found_cascade = _destructive_sites(path.read_text(), relative)
            filesystem.update(found_fs)
            cascades.update(found_cascade)
    return filesystem, cascades, read


def test_no_undeclared_path_deletes_a_file() -> None:
    """REQ-090's other half: the filesystem.

    `purge_derived` recursively removes a directory. `mirror._prune` walks a
    caller-supplied root and unlinks everything not in a `keep` set. Both are
    correct today, and both are correct by *prose* — the guard the project
    relies on for this exact promise could not see either of them, and could not
    see the eighth one either.
    """
    filesystem, _, read = _scan_destructive()

    assert read > 50, (
        f"only {read} modules were read — the package scan has stopped matching "
        "the tree, and this guard is inspecting almost nothing"
    )

    undeclared = {
        site: why for site, why in filesystem.items() if site not in FILESYSTEM_DELETION
    }
    assert not undeclared, (
        "these delete files and are not declared (REQ-090):\n  "
        + "\n  ".join(f"{site}: {why}" for site, why in sorted(undeclared.items()))
        + "\nRevoke, tombstone, or supersede. If the bytes genuinely are derived "
        "and reproducible, add the site to FILESYSTEM_DELETION with the argument "
        "for why it is not an original — and expect to be asked."
    )


def test_the_filesystem_exemptions_do_not_outlive_their_call_sites() -> None:
    """It may only shrink. A stale entry is a hole left open for the next author,
    exactly as in `test_the_vault_exception_stays_one_file`."""
    filesystem, _, _ = _scan_destructive()
    stale = sorted(set(FILESYSTEM_DELETION) - set(filesystem))
    assert not stale, (
        "these no longer delete anything; remove them from FILESYSTEM_DELETION "
        "so the next site that does is caught:\n  " + "\n  ".join(stale)
    )
    # Every exemption is an argument, not a name. An empty reason is a name.
    thin = sorted(site for site, why in FILESYSTEM_DELETION.items() if len(why) < 40)
    assert not thin, f"these exemptions carry no argument: {thin}"


def test_no_relationship_cascades_a_delete() -> None:
    """The quietest of the three threats the module docstring names.

    `cascade="all, delete-orphan"` on a relationship, or `ondelete="CASCADE"` on
    a foreign key, deletes rows nobody wrote a delete for — and neither is
    visible to a pattern list looking for `DELETE FROM`. Segments are superseded
    and tags are removed by timestamp precisely so that nothing has to cascade.
    """
    _, cascades, _ = _scan_destructive()
    undeclared = {site: why for site, why in cascades.items() if site not in ORM_CASCADES}
    assert not undeclared, (
        "these declare a deleting cascade, so rows will be removed without any "
        "code asking for it (REQ-090):\n  "
        + "\n  ".join(f"{site}: {why}" for site, why in sorted(undeclared.items()))
        + "\nUse `removed_at` / `superseded_at` / `released_at` instead — the "
        "pattern the rest of the schema uses."
    )


def test_the_guard_catches_the_two_spellings_it_was_blind_to() -> None:
    """The guard, tested on itself. A detection nobody has seen fire is a
    detection nobody should trust — Phase 7 learned this the expensive way."""
    filesystem, cascades = _destructive_sites(
        textwrap.dedent(
            """
            import shutil
            import sqlalchemy as sa
            from sqlalchemy.orm import relationship

            def nightly_cleanup(root):
                shutil.rmtree(root / 'old')

            def tidy(path):
                path.unlink()

            class Thing:
                pages = relationship('Page', cascade='all, delete-orphan')
                owner = sa.Column(sa.ForeignKey('x.id', ondelete='CASCADE'))
            """
        ),
        "api/pretend.py",
    )
    assert filesystem == {
        "api/pretend.py::nightly_cleanup": "calls rmtree()",
        "api/pretend.py::tidy": "calls unlink()",
    }, filesystem
    assert set(cascades) == {"api/pretend.py::<module>"}, cascades

    # And it does not fire on the spellings that remove nothing.
    harmless, no_cascade = _destructive_sites(
        textwrap.dedent(
            """
            import sqlalchemy as sa

            class Thing:
                owner = sa.Column(sa.ForeignKey('x.id', ondelete='SET NULL'))
                other = sa.Column(sa.ForeignKey('y.id', ondelete='RESTRICT'))

            def read(path):
                return path.read_text()
            """
        ),
        "api/pretend.py",
    )
    assert harmless == {} and no_cascade == {}


def test_the_mirror_prune_stays_inside_the_tree_it_was_given() -> None:
    """The one exempted function that walks a caller-supplied directory.

    `_prune(root, keep)` unlinks everything under `root` that is not in `keep`.
    Pointed at the wrong directory — or handed a `keep` set built from
    differently-normalised paths — it is a recursive delete over user data. The
    exemption above argues it is safe; this asserts the one property that
    argument rests on, which is that it cannot reach outside `root`.
    """
    import tempfile

    from api.export import mirror

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / "mirror"
        (root / "2024").mkdir(parents=True)
        kept = root / "2024" / "keep-me.pdf"
        doomed = root / "2024" / "orphan.pdf"
        kept.write_text("kept")
        doomed.write_text("orphan")

        # A real original, sitting beside the mirror rather than inside it.
        outside = base / "blobs" / "ab"
        outside.mkdir(parents=True)
        original = outside / "an-original.pdf"
        original.write_text("irreplaceable")

        removed = mirror._prune(root, {kept})

        assert kept.is_file(), "_prune removed an entry it was told to keep"
        assert not doomed.exists(), "_prune kept an entry no document maps to"
        assert removed >= 1
        assert original.is_file(), (
            "_prune deleted a file outside the root it was given — the mirror is "
            "exempt from REQ-090 only because everything it touches is a derived "
            "hardlink inside its own tree"
        )
        assert original.read_text() == "irreplaceable"


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
