"""Every audited action is undoable or says why not (CR-058, REQ-067).

> Automate by default; make every automated decision cheap to inspect and one
> click to reverse.

Undo is the trust surface, and the kill criterion is loss of trust in the
automated filing. An action that is *audited* but silently not undoable is a
promise the Why panel makes and the code does not keep — and nothing failed
when one appeared, because what was undoable lived in a four-entry literal in
`api/undo.py` while roughly fifty distinct actions were being recorded.

So this is the guard in the spirit of `tests/test_no_destructive_paths.py`: it
walks `api/` and `worker/` for the action names actually passed to
`api.audit.record`, and fails on any that is in neither `undo.REGISTRY` nor
`undo.NOT_UNDOABLE`. Adding an action forces the author to answer the question
once, in one place, in a sentence someone else can read.

It fails in both directions on purpose. A stale entry — a reason for an action
nothing records any more — is how the map drifts into fiction, and a fiction
about what is reversible is the thing this file exists to prevent.
"""

import ast
from pathlib import Path

from api import undo

ROOT = Path(__file__).resolve().parent.parent
SEARCHED = ("api", "worker")


def _action_constants(node: ast.AST) -> list[str]:
    """The string values an `action=` argument can take at one call site.

    A conditional is two actions, not none: `api/routers/imports.py` records
    `"import_to_vault" if to_vault else "import_not_to_vault"`, and a scan that
    only understood plain literals reported both as absent from the codebase
    while both were being written to the audit table.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _action_constants(node.body) + _action_constants(node.orelse)
    return []


def audited_actions() -> dict[str, list[str]]:
    """Action name -> where it is recorded. The whole audited surface."""
    found: dict[str, list[str]] = {}

    def note(action: str, where: str) -> None:
        found.setdefault(action, []).append(where)

    for package in SEARCHED:
        for path in sorted((ROOT / package).rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            relative = path.relative_to(ROOT)
            for node in ast.walk(tree):
                # `api.audit.record(...)`, however it was imported. Restricted
                # to that call so argparse's `action="store_true"` — a
                # different meaning of the same keyword — stays out.
                if isinstance(node, ast.Call):
                    name = (
                        node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", None)
                    )
                    if name == "record":
                        for keyword in node.keywords:
                            if keyword.arg != "action":
                                continue
                            for value in _action_constants(keyword.value):
                                note(value, f"{relative}:{node.lineno}")
                # `api/segments.replace(..., action: str = "segment")` — the
                # action is a parameter with a default, and the default is the
                # name every ordinary segmentation is recorded under.
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    args = node.args
                    # `defaults` covers positional-only and ordinary arguments
                    # together, right-aligned; `kw_defaults` is one-to-one with
                    # the keyword-only ones.
                    positional = args.posonlyargs + args.args
                    padding = [None] * (len(positional) - len(args.defaults))
                    pairs = list(
                        zip(
                            positional + args.kwonlyargs,
                            padding + list(args.defaults) + list(args.kw_defaults),
                            strict=True,
                        )
                    )
                    for argument, default in pairs:
                        if argument.arg != "action" or default is None:
                            continue
                        for value in _action_constants(default):
                            note(value, f"{relative}:{node.lineno}")
    return found


def test_the_scan_finds_the_audited_surface():
    """The guard's own floor.

    `tests/test_permission_boundary.py`'s route guard once passed while
    examining zero routes. A scan that silently matches nothing agrees with
    every registry there is.
    """
    found = audited_actions()
    assert len(found) >= 50, f"only found {len(found)} audited actions: {sorted(found)}"
    assert "classify" in found and "vault_move_in" in found


def test_every_audited_action_is_undoable_or_says_why_not():
    found = audited_actions()
    unclassified = {
        action: sites
        for action, sites in found.items()
        if action not in undo.REGISTRY and action not in undo.NOT_UNDOABLE
    }
    assert not unclassified, (
        "these audited actions are in neither api/undo.REGISTRY nor "
        "api/undo.NOT_UNDOABLE, so nothing in the application knows whether "
        "they can be walked back:\n"
        + "\n".join(f"  {action} — {sites[0]}" for action, sites in sorted(unclassified.items()))
        + "\n\nAdd it to REGISTRY with the endpoint that reverses it, or to "
        "NOT_UNDOABLE with the reason it has no reverse."
    )


def test_the_registry_does_not_describe_actions_nothing_records():
    """A reason for an action that no longer exists is a fiction about undo."""
    found = audited_actions()
    declared = set(undo.REGISTRY) | set(undo.NOT_UNDOABLE)
    stale = declared - set(found)
    assert not stale, (
        f"api/undo.py claims to know about {sorted(stale)}, which nothing in "
        "api/ or worker/ records any more. Remove the entries."
    )


def test_no_action_is_both_undoable_and_not():
    both = set(undo.REGISTRY) & set(undo.NOT_UNDOABLE)
    assert not both, f"{sorted(both)} appear in both maps"


def test_every_reason_is_a_sentence():
    """A reason is what makes the second map honest rather than an allow-list."""
    for action, reason in undo.NOT_UNDOABLE.items():
        assert len(reason.split()) >= 5, f"{action}: {reason!r} is not a reason"
        assert reason.rstrip().endswith("."), f"{action}: {reason!r} is not a sentence"


def test_every_undoable_action_names_a_route_that_exists():
    """The registry points at endpoints, so a renamed one must not go unnoticed."""
    from api.main import app

    # Read from the schema rather than `app.routes`: an included router is a
    # container there, not an endpoint, so walking the list finds four paths
    # and agrees with anything.
    routes = set(app.openapi()["paths"])
    assert len(routes) > 100, "the schema is not being read"

    for action, entry in undo.REGISTRY.items():
        assert entry.route in routes, (
            f"{action} claims to be undone by {entry.route}, which is not a "
            "route this application serves"
        )


def test_the_generic_path_restores_only_registered_fields():
    """`UNDOABLE` is a view of the registry, not a second list of its own."""
    assert set(undo.UNDOABLE) == {
        action
        for action, entry in undo.REGISTRY.items()
        if entry.route == undo.DOCUMENT_UNDO
    }
    for action, fields in undo.UNDOABLE.items():
        assert fields, f"{action} is on the generic path and restores nothing"
        assert undo.REGISTRY[action].entity_type == "document"


def test_whole_operation_reversals_declare_no_fields():
    """Bulk, merge and segment read their own manifests; a field list here
    would be a second, unread description of what they restore."""
    for action, entry in undo.REGISTRY.items():
        if entry.route != undo.DOCUMENT_UNDO:
            assert entry.fields == (), f"{action} declares fields it never uses"
