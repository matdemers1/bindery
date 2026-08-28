"""The layering rule, enforced (CLAUDE.md).

> `worker/` may import `api/`. `api/` must **never** import `worker/`.

This is not architectural neatness. The api image ships `api/` and `alembic/`
and nothing else, so an `api → worker` import is a `ModuleNotFoundError` in
production on a code path the test suite runs green — the test image is built
from the `dev` stage, which copies the whole tree, so it never notices.

That is exactly what happened: every backlog-import endpoint imported
`worker.backlog.session` inside the function body, passed every test, and would
have raised on the deployed host the first time anyone opened the Import screen.
The fix was to move the module to `api/`, which is the only package both images
carry. This test is here so it cannot happen again.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _imports_worker(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name.split(".")[0] == "worker"]
        elif isinstance(node, ast.ImportFrom):
            # Function-local imports are the dangerous case, not the safe one:
            # they defer the failure to request time. ast.walk finds both.
            if node.module and node.module.split(".")[0] == "worker":
                found.append(node.module)
    return found


def test_api_never_imports_worker() -> None:
    offences: list[str] = []
    for path in sorted((ROOT / "api").rglob("*.py")):
        for module in _imports_worker(ast.parse(path.read_text())):
            offences.append(f"{path.relative_to(ROOT)}: imports {module}")

    assert not offences, (
        "api/ must not import worker/ — the api image does not contain it, so "
        "this is a ModuleNotFoundError in production that the test image hides. "
        "Move the shared code into api/:\n  " + "\n  ".join(offences)
    )


def test_the_api_image_would_carry_everything_api_imports() -> None:
    """The Dockerfile is the other half of the rule; keep them in step."""
    dockerfile = (ROOT / "infra" / "Dockerfile.api").read_text()
    runtime = dockerfile.split("FROM base AS runtime")[1].split("FROM base AS dev")[0]
    assert "COPY api/" in runtime
    assert "COPY worker/" not in runtime, (
        "if the api image starts shipping worker/, this rule needs rewriting "
        "rather than quietly bypassing"
    )
