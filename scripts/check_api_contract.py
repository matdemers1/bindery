#!/usr/bin/env python3
"""The wire contract is written twice. This is what stops the two drifting.

    python scripts/check_api_contract.py

`api/schemas.py` declares the response models; `web/src/api.ts` declares a
hand-written TypeScript mirror of them. They agree today — every field the client
reads exists on the server — and nothing in the pipeline was checking that
(CR-059). `tsc --noEmit` proves the client is internally consistent, not that it
matches the server, so a renamed or narrowed response field passes lint, unit,
integration and e2e and arrives in the browser as `undefined` on whichever screen
the e2e specs happen not to assert on. On a UI whose whole job is to make an
automated decision inspectable, a blank field on the Why panel is
indistinguishable from an AI that had nothing to say.

**What is checked, and what is not.** For every TypeScript interface that pairs
with a Pydantic model, every field the client declares must exist on the server.
The other direction is deliberately not checked: the client is free to read a
subset, and most screens do.

**Why static parsing rather than the OpenAPI document.** `/api/openapi.json`
needs a running app, an importable settings object and a database URL, none of
which the lint gate has — and the lint gate is where this belongs, because it is
the cheapest one and this is the failure it is worth failing fast on. Both files
are parsed on disk instead: `ast` for the Python, a small scanner for the
TypeScript. Nothing here imports the application.

**Pairing.** By name, allowing for the naming conventions each side uses:
`DocumentOut` pairs with `Document`, `SegmentIn` with `Segment`. Renames that
those rules do not cover are listed in `ALIASES`, and interfaces with no server
model at all are listed in `UNPAIRED` with the reason. Both lists are exhaustive:
a new interface that is neither paired nor listed fails this check, so the hole
cannot be widened silently — which is the failure mode the check exists for.
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API = REPO / "api"
CLIENT = REPO / "web" / "src" / "api.ts"

# Suffixes the server puts on a wire model and the client does not.
SUFFIXES = ("Out", "In", "Response", "Request", "Result", "Record", "Report")

# Renames the suffix rule cannot reach. Each is a claim that these two describe
# the same JSON, and the field check below is what holds that claim to account.
ALIASES = {
    "ApiTokenRecord": "ApiTokenOut",
    "AskAnswer": "AskOut",
    "AskCitation": "CitationOut",
    "AssetRef": "AssetOut",
    "AuditEventRecord": "AuditEventOut",
    "BackupResult": "BackupOut",
    "ClassificationRecord": "ClassificationOut",
    "CorrespondentRef": "CorrespondentOut",
    "DocumentEditResult": "DocumentEditOut",
    "ExportResult": "ExportOut",
    "FieldProvenanceRecord": "FieldProvenanceOut",
    "FieldSourceRef": "FieldSourceOut",
    "HealthAlert": "AlertOut",
    "HealthBadge": "BadgeOut",
    "IntegrityReport": "IntegrityOut",
    "IssuedApiToken": "ApiTokenIssuedOut",
    "MirrorResult": "MirrorOut",
    "PageSummary": "PageOut",
    "RuleRecord": "RuleOut",
    "SegmentInput": "SegmentIn",
    "TagRef": "TagOut",
    "VaultSearchResults": "VaultSearchOut",
}

# Client types with no Pydantic model behind them. Each needs a reason, so that
# adding to this list is a decision rather than a way of getting the check to be
# quiet — and the list is itself worth reading: every entry is a response the
# server builds as a dict rather than as a declared model.
UNPAIRED = {
    "AdminInvitation": "the admin invitation list is built as a dict in api/routers/admin.py",
    "AuditFilters": "query parameters the client sends, not a response body",
    "InvitePreview": "the unauthenticated invite preview is assembled in the router",
    "PageBoxes": "word boxes are served straight from the stored OCR JSON",
    "SearchParams": "query parameters the client sends, not a response body",
    "ServiceBuild": "api/version.py reports builds as a dict",
    "TimelineEntry": "an element of AssetTimelineOut's list, not a model of its own",
    "VersionReport": "api/version.py reports the schema state as a dict",
    "Word": "one entry inside PageBoxes",
}


def _fields_of_class(node: ast.ClassDef) -> set[str]:
    return {
        statement.target.id
        for statement in node.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    }


def server_models() -> dict[str, set[str]]:
    """Every Pydantic model in `api/`, with its inherited fields folded in.

    Pydantic models only, and that restriction is load-bearing. `api/` also
    holds dataclasses and a `BaseSettings` with names the client happens to
    share — `Settings`, `HealthPanel`, `VaultItem` — and pairing an interface
    with one of those compares it against a shape that never goes over the wire,
    which produces a page of failures that are all the checker's own fault.
    """
    declared: dict[str, tuple[list[str], set[str]]] = {}
    for path in sorted(API.rglob("*.py")):
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [
                base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                for base in node.bases
            ]
            # Fields merge rather than replace: two same-named models in
            # different routers are rare, and a union is the answer that cannot
            # produce a false failure.
            previous = declared.get(node.name)
            if previous is not None:
                declared[node.name] = (previous[0] + bases, previous[1] | _fields_of_class(node))
            else:
                declared[node.name] = (bases, _fields_of_class(node))

    # A model is a wire model if it inherits `BaseModel`, directly or through
    # another one. Repeated to a fixpoint because a subclass can be declared
    # above its parent, or in a different file.
    wire = {name for name, (bases, _) in declared.items() if "BaseModel" in bases}
    while True:
        grown = {
            name
            for name, (bases, _) in declared.items()
            if name not in wire and any(base in wire for base in bases)
        }
        if not grown:
            break
        wire |= grown

    def resolve(name: str, seen: tuple[str, ...] = ()) -> set[str]:
        bases, fields = declared[name]
        inherited: set[str] = set()
        for base in bases:
            if base in declared and base not in seen:
                inherited |= resolve(base, (*seen, name))
        return fields | inherited

    return {name: resolve(name) for name in sorted(wire)}


INTERFACE = re.compile(r"export\s+interface\s+(\w+)(?:\s+extends\s+([\w,\s]+?))?\s*\{")


def _body_after(text: str, opening: int) -> str:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
    return ""


def client_interfaces() -> dict[str, set[str]]:
    """Every `export interface` in the client, with its `extends` folded in.

    Only top-level properties are collected: a nested object literal is the
    shape of a field, and its field is what the server has to declare.
    """
    text = CLIENT.read_text()
    declared: dict[str, tuple[list[str], set[str]]] = {}
    for match in INTERFACE.finditer(text):
        body = _body_after(text, match.end() - 1)
        body = re.sub(r"//.*", "", body)
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        fields: set[str] = set()
        depth = 0
        for line in body.splitlines():
            if depth == 0:
                property_name = re.match(r"(\w+)\??\s*:", line.strip())
                if property_name is not None:
                    fields.add(property_name.group(1))
            depth += line.count("{") - line.count("}")
        extends = [name.strip() for name in (match.group(2) or "").split(",") if name.strip()]
        declared[match.group(1)] = (extends, fields)

    def resolve(name: str, seen: tuple[str, ...] = ()) -> set[str]:
        extends, fields = declared[name]
        inherited: set[str] = set()
        for parent in extends:
            if parent in declared and parent not in seen:
                inherited |= resolve(parent, (*seen, name))
        return fields | inherited

    return {name: resolve(name) for name in declared}


def pair_up(
    server: dict[str, set[str]], client: dict[str, set[str]]
) -> tuple[dict[str, str], list[str]]:
    """Which interface answers which model, and which answer nothing."""
    by_wire_name: dict[str, list[str]] = {}
    for name in server:
        by_wire_name.setdefault(name, []).append(name)
        for suffix in SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                by_wire_name.setdefault(name[: -len(suffix)], []).append(name)

    pairs: dict[str, str] = {}
    unpaired: list[str] = []
    for name in sorted(client):
        if name in ALIASES:
            pairs[name] = ALIASES[name]
        elif name in by_wire_name:
            # Prefer the exact name, then the shortest suffix-stripped candidate.
            candidates = by_wire_name[name]
            pairs[name] = name if name in candidates else min(candidates, key=len)
        else:
            unpaired.append(name)
    return pairs, unpaired


def check(
    server: dict[str, set[str]] | None = None,
    client: dict[str, set[str]] | None = None,
) -> list[str]:
    """Every way the two halves disagree, as sentences. Empty means they agree.

    Both sides are injectable so that a test can prove this can fail. A
    contract check that has only ever been run against a repository that already
    agrees is a check nobody has seen work.
    """
    server = server_models() if server is None else server
    client = client_interfaces() if client is None else client
    pairs, unpaired = pair_up(server, client)
    problems: list[str] = []

    for interface, model in pairs.items():
        if model not in server:
            problems.append(
                f"ALIASES maps {interface!r} to {model!r}, and no such model exists in api/"
            )
            continue
        missing = sorted(client[interface] - server[model])
        if missing:
            problems.append(
                f"web/src/api.ts `{interface}` reads {missing} — api/schemas.py "
                f"`{model}` does not declare {'them' if len(missing) > 1 else 'it'}. "
                "The screen gets `undefined`, silently"
            )

    for interface in unpaired:
        if interface not in UNPAIRED:
            problems.append(
                f"web/src/api.ts `{interface}` matches no Pydantic model and is not "
                "in UNPAIRED. Name it after its model, add it to ALIASES, or record "
                "in UNPAIRED why the server has no model for it"
            )

    for interface, reason in sorted(UNPAIRED.items()):
        if interface not in client:
            problems.append(
                f"UNPAIRED still excuses `{interface}` ({reason}), which no longer "
                "exists in web/src/api.ts. Delete the entry"
            )

    for interface in sorted(ALIASES):
        if interface not in client:
            problems.append(
                f"ALIASES still maps `{interface}`, which no longer exists in "
                "web/src/api.ts. Delete the entry"
            )
    return problems


def main() -> int:
    problems = check()
    if problems:
        print("the API contract has drifted:\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nThe server is the contract. Change web/src/api.ts to match "
            "api/schemas.py, not the other way round."
        )
        return 1
    server, client = server_models(), client_interfaces()
    pairs, _ = pair_up(server, client)
    print(
        f"api contract: {len(pairs)} of {len(client)} client types checked against "
        f"{len(server)} server models; no field is missing server-side"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
