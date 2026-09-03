"""The README, and the Makefile it describes, kept true by something (CR-083).

The README is the first document a stranger opens and the one a future you judges
the project's shape by. Every claim in it was checkable in under a minute and
several were wrong: the status said Phase 3, the stack table listed ingress as
"Cloudflare Tunnel + Access" ten months after ADR-008 removed Access, the
Cloudflare Tunnel was listed as an open task two days after it went live, the
Makefile was described as "twenty lines" when it was 118 with 28 targets, and the
ADR and phase counts were half of the real ones. A document that is wrong in five
checkable places trains its reader to stop checking the sixth.

The worst of them was the ingress row, because it is not merely stale: it tells a
reader there is a second gate in front of the login page when there is not, and
`api/auth/throttle.py` is load-bearing precisely because there is not.

What is asserted here is only what can be: counts and cross-references, never
prose. `tests/test_docs.py` does the same job for the in-app guides, and
`tests/test_operability.py` for the runbooks.

The last two tests are the same idea pointed at the pipeline: a gate the
documentation promises has to be a gate CI actually runs, and a posture the
project has chosen has to be written down somewhere a stranger will find it.

**One caveat worth knowing.** The compose test service bind-mounts `api/`,
`tests/`, `docs/` and the rest, and does *not* mount `README.md`, `Makefile` or
`CLAUDE.md` — they arrive through the image's `COPY . .`. So `make test` reads
whatever they were when the image was last built, while CI's unit job runs pytest
straight against the checkout and reads the real thing. If a change here looks
like it did not take effect, rebuild: `make build`.
"""

import json
import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
MAKEFILE = REPO / "Makefile"
WORKFLOW = REPO / ".github" / "workflows" / "build.yml"
CAPTURE = REPO / "scripts" / "capture-screens.py"
VITE = REPO / "web" / "vite.config.ts"

TARGET = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*):", re.M)


def makefile_targets() -> set[str]:
    return set(TARGET.findall(MAKEFILE.read_text()))


def phony_targets() -> set[str]:
    for line in MAKEFILE.read_text().splitlines():
        if line.startswith(".PHONY:"):
            return set(line.removeprefix(".PHONY:").split())
    raise AssertionError("the Makefile has no .PHONY line")


def test_the_readme_does_not_promise_a_perimeter_that_is_gone() -> None:
    """The sharpest of the stale claims. Access was removed on 2026-08-30
    (ADR-008); a reader told to expect it in front of the login page reads
    "the login page is directly exposed" as a bug report rather than as the
    state that makes the login throttle the only thing there is."""
    text = README.read_text()
    assert "Tunnel + Access" not in text, (
        "the stack table still lists ingress as 'Cloudflare Tunnel + Access'. "
        "Access was removed on 2026-08-30 (ADR-008) and the tunnel is now the "
        "only thing in front of the login page"
    )
    assert "ADR-008" in text, (
        "the README describes the ingress path without naming the decision that "
        "removed Access, so the next reader has no way to know it ever existed"
    )


def test_every_make_command_the_readme_names_exists() -> None:
    """A renamed target leaves the README pointing at a command that prints
    'No rule to make target', which is how a stranger's first ten minutes go."""
    text = README.read_text()
    # Backticked, or a line of a fenced block. Bare prose is not a command: the
    # README is allowed to contain the word "make".
    named = set(re.findall(r"`make ([a-z][a-z0-9-]*)", text))
    named |= set(re.findall(r"^make ([a-z][a-z0-9-]*)", text, re.M))
    missing = sorted(named - makefile_targets())
    assert not missing, (
        f"the README gives commands the Makefile does not have: {missing}. "
        "Rename them here, or add the targets"
    )


def test_the_phony_list_and_the_targets_are_the_same_set() -> None:
    """Both directions have already been wrong. `screenshots` was a real target
    missing from `.PHONY` (CR-081); `typecheck` was in `.PHONY` with no target
    behind it at all (CR-089) — a review left the entry and dropped the rule, and
    nothing said so."""
    targets, phony = makefile_targets(), phony_targets()
    assert not (phony - targets), (
        f".PHONY names targets that do not exist: {sorted(phony - targets)}. "
        "A phony entry with no rule is a command that looks supported and is not"
    )
    assert not (targets - phony), (
        f"these targets are missing from .PHONY: {sorted(targets - phony)}"
    )


def test_the_readme_does_not_describe_the_makefile_by_its_length() -> None:
    """"read the Makefile, it is twenty lines" was written when it was, and was
    six times wrong by the time anyone read it. The 24 `## …` comments it carries
    are the durable answer, and something has to render them."""
    text = README.read_text()
    assert "twenty lines" not in text, (
        "the README still describes the Makefile by a line count. It is "
        f"{len(MAKEFILE.read_text().splitlines())} lines with "
        f"{len(makefile_targets())} targets"
    )
    assert "make help" in text, "the README never tells the reader how to list the commands"
    assert "help" in makefile_targets(), (
        "the README sends the reader to `make help` and there is no help target"
    )
    documented = re.findall(r"^([a-zA-Z][a-zA-Z0-9_-]*):.*## ", MAKEFILE.read_text(), re.M)
    assert len(documented) >= 20, (
        "the `## …` comments have gone; either they or the help target should go, "
        "not one without the other"
    )


def test_the_readme_counts_nothing_this_repository_cannot_check() -> None:
    """The ADR, phase-plan and REQ counts all lived here and all rotted, and no
    test could ever have caught them: the vault is not part of this repository.
    The fix is not a better number, it is not carrying one."""
    text = README.read_text()
    counted = re.findall(
        r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"thirteen|fourteen|fifteen)\s+(REQs?|[Pp]hase [Pp]lans?|ADRs?|phases)\b",
        text,
    )
    assert not counted, (
        f"the README counts {counted}, which lives in the vault and cannot be "
        "checked from this repository. Point at `D3 Cloud Vault/Bindery/` instead"
    )
    assert not re.search(r"ADR-0*1\s*…\s*ADR-\d+", text), (
        "the README enumerates an ADR range. It was 'ADR-001 … ADR-006' against "
        "thirteen of them"
    )


def test_the_readme_gives_a_way_in_that_does_not_need_a_tunnel() -> None:
    """The quick start ended at `make create-user` and then said to reach the
    stack through Cloudflare. With no published host port (REQ-104) and no tunnel
    token, a developer could build and migrate the whole stack and then had no
    way to open the application — and the only recipe that works was a comment
    inside web/vite.config.ts."""
    text = README.read_text()
    assert "socat" in text, (
        "the README has no way to reach the app without a tunnel. There are no "
        "published ports by invariant, so this is not an optional aside"
    )
    assert "REQ-104" in text, "nothing says *why* there is no host port to browse to"
    assert "BINDERY_API" in text and "BINDERY_API" in VITE.read_text(), (
        "the local recipe names an override the dev server does not read, or the "
        "dev server's override was renamed and the README kept the old name"
    )


def test_the_screenshot_command_is_the_one_that_actually_works() -> None:
    """CR-081. `make screenshots` ran the capture script on the host against
    `http://localhost:8080`, a port that cannot exist (REQ-104), while the
    invocation that works lived only in a compose comment. Capture has to run
    inside the network, and the URL has to be `localhost` — the session cookie is
    `Secure`, and `localhost` is the one origin Chrome trusts without TLS."""
    recipe = MAKEFILE.read_text().split("screenshots:")[1]
    assert "--profile screenshots" in recipe, (
        "`make screenshots` no longer runs the compose service. On the host it "
        "reaches nothing: no service publishes a port"
    )
    # Comments are exempt: both files explain at length why that port cannot
    # exist, and the explanation is the part worth keeping.
    for path in (MAKEFILE, CAPTURE):
        code = "\n".join(
            line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
        )
        assert "localhost:8080" not in code, (
            f"{path.name} still defaults to a host port. REQ-104 says none exists"
        )


def test_the_readme_describes_the_pipeline_ci_actually_runs() -> None:
    """It said four gates while the workflow had five, which matters because the
    fifth is the one that publishes the images the household's archive pulls."""
    jobs = list(yaml.safe_load(WORKFLOW.read_text())["jobs"])
    text = README.read_text()
    missing = [job for job in jobs if job not in text]
    assert not missing, f"the README's CI description never mentions {missing}"
    words = {4: "four", 5: "five", 6: "six", 7: "seven"}
    assert f"{words[len(jobs)]} gates" in text, (
        f"the workflow has {len(jobs)} jobs and the README does not say so"
    )


def test_the_gates_the_documentation_promises_are_gates_ci_runs() -> None:
    """Both were added by this round, and both belong in `lint` — the cheapest
    gate, which is the sequencing argument the workflow already makes."""
    lint = yaml.safe_load(WORKFLOW.read_text())["jobs"]["lint"]["steps"]
    commands = " ".join(step.get("run", "") for step in lint)
    for gate in ("scripts/check_api_contract.py", "scripts/typecheck.py"):
        assert gate in commands, (
            f"{gate} is not run by CI's lint job. A checker nothing invokes is "
            "the state this repository has already been in once"
        )


def test_the_type_checking_posture_is_written_down() -> None:
    """CR-089's defect was the silence, not the absence of type checking. mypy
    sat in the `dev` extra with no configuration, no gate and nothing anywhere
    saying whether that was a decision — so the next engineer had to spend an
    afternoon discovering it was never wired up."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    declared = " ".join(pyproject["project"]["optional-dependencies"]["dev"])
    configured = "mypy" in pyproject.get("tool", {})
    gated = "typecheck" in makefile_targets()
    claude = (REPO / "CLAUDE.md").read_text()

    if "mypy" not in declared:
        # Dropping it is a legitimate answer to the finding — but only out loud.
        assert "mypy" in claude, (
            "mypy was removed from the dev extra and nothing says so. Whichever "
            "way this goes, CLAUDE.md has to carry the decision"
        )
        return

    assert configured, "mypy is a dependency with no [tool.mypy] section, again"
    assert gated, "mypy is configured and nothing runs it, again"
    assert "mypy" in claude and "scripts/mypy-baseline.json" in claude, (
        "CLAUDE.md's Conventions section does not describe the type-checking "
        "posture. The silence was the defect"
    )
    baseline = json.loads((REPO / "scripts" / "mypy-baseline.json").read_text())
    assert baseline["files"], "the baseline is empty, so the ratchet holds nothing"
    assert baseline["total"] == sum(
        sum(codes.values()) for codes in baseline["files"].values()
    ), "the baseline's total disagrees with its own contents"
