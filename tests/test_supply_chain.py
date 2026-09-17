"""What is installed, what watches it, and how anyone finds out the worker stalled.

Three properties, none of which any other test would notice breaking:

1. **The lock is real and everything installs under it.** Without it, the set of
   packages CI tests and the set the published images run are two independent
   resolutions of the same unbounded names — a green pipeline that says nothing
   about the artifact it produced.
2. **Something scans, on a cadence, and fails.** An advisory against
   `cryptography`, `starlette` or `python-multipart` has to reach somebody; the
   api's login page faces the open internet (invariant 9).
3. **The worker's liveness is asserted, not assumed.** A hung worker and an idle
   worker both report `Up` with no logs. `worker/runner.py` used to carry a
   docstring claiming "the ZimaOS healthcheck restarts a dead container" when no
   such healthcheck existed anywhere — a comment describing a mechanism that had
   never been deployed, on the one process whose silence is invisible.

Read as file contents rather than by running anything, so this is cheap and runs
in the default suite.
"""

import re
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from worker import runner

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock"
COMPOSE = ROOT / "infra" / "docker-compose.yml"
ZIMAOS = ROOT / "infra" / "zimaos" / "bindery.zimaos.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
DOCKERFILES = [ROOT / "infra" / "Dockerfile.api", ROOT / "infra" / "Dockerfile.worker"]


def _locked() -> dict[str, Version]:
    pinned: dict[str, Version] = {}
    for raw in LOCK.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"requirements.lock is not fully pinned: {line!r}"
        name, _, version = line.partition("==")
        assert "[" not in name, (
            f"{line!r}: pip refuses extras in a constraints file, and this is one"
        )
        key = canonicalize_name(name)
        assert key not in pinned, f"{name} is pinned twice in requirements.lock"
        pinned[key] = Version(version)
    return pinned


def _declared() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = list(project["dependencies"])
    for group in project["optional-dependencies"].values():
        declared += group
    return [Requirement(text) for text in declared]


def test_every_declared_dependency_is_pinned_by_the_lock() -> None:
    """Every declared dependency resolves to one known version — by the lock, or by its URL.

    A requirement written as a direct reference is already pinned harder than a version line:
    `d3auth-client` names a commit of the provider's repository and pip records its hash. It is
    deliberately *not* in the lock, because pip refuses to resolve a direct reference that also
    appears as a constraint.
    """
    pinned = _locked()
    missing = [
        requirement.name
        for requirement in _declared()
        if canonicalize_name(requirement.name) not in pinned and requirement.url is None
    ]
    assert not missing, (
        "declared in pyproject.toml but absent from requirements.lock, so it "
        "installs at whatever version PyPI serves that minute: "
        f"{', '.join(sorted(missing))}. Run `make lock`."
    )

    # And a direct reference has to name something immutable: a branch or a tag can be moved
    # under us, a commit cannot.
    floating = [
        requirement.name
        for requirement in _declared()
        if requirement.url and not re.search(r"/(archive|releases/download)/[0-9a-f]{40}\.", requirement.url)
    ]
    assert not floating, (
        "pinned by URL to something that can move; name a commit instead: "
        f"{', '.join(sorted(floating))}"
    )


def test_the_lock_satisfies_the_bounds_pyproject_declares() -> None:
    """The two bounded requirements are bounded because their behaviour is parsed.

    `ocrmypdf` — normalize.py branches on its exit codes and stderr wording.
    `anthropic` — ask.py depends on the shape of its citations. A `make lock`
    that quietly carried either across a major would not fail a build; it would
    change what the pipeline believes happened.
    """
    pinned = _locked()
    violations = []
    for requirement in _declared():
        if not requirement.specifier:
            continue
        version = pinned[canonicalize_name(requirement.name)]
        if not requirement.specifier.contains(version, prereleases=True):
            violations.append(f"{requirement} is locked at {version}")
    assert not violations, (
        "requirements.lock contradicts the bounds pyproject.toml declares: "
        + "; ".join(violations)
    )


def test_every_pip_install_runs_under_the_lock() -> None:
    """Both stages of both Dockerfiles, or the image is not what was tested.

    This also fixes the buildx cache: the lock's content hash is part of the
    dependency layer's key, so a dependency change busts the pip layer and an
    application change does not. Before it, `cache-from: type=gha` froze one
    resolution indefinitely and a security fix could not reach a published image
    at all without somebody editing pyproject.toml to bust a cache.
    """
    for dockerfile in DOCKERFILES:
        text = dockerfile.read_text()
        installs = [
            line for line in text.splitlines() if "pip install" in line and "requirements" in line
        ]
        assert installs, f"{dockerfile.name}: no pip install of a requirements file found"
        for line in installs:
            assert "-c requirements.lock" in line, (
                f"{dockerfile.name}: `{line.strip()}` installs without the lock"
            )
        assert "COPY pyproject.toml requirements.lock" in text, (
            f"{dockerfile.name}: the lock must be COPYd with pyproject.toml — that is "
            "what puts its hash in the dependency layer's cache key"
        )


def test_ci_installs_under_the_lock_and_scans_both_ecosystems() -> None:
    workflow = WORKFLOW.read_text()

    for line in workflow.splitlines():
        if "pip install" in line and "requirements.txt" in line:
            assert "-c requirements.lock" in line, (
                f"CI installs without the lock: `{line.strip()}` — it would then test "
                "a different resolution from the one the images job publishes"
            )

    assert "pip-audit" in workflow, "nothing scans the Python tree for advisories"
    assert "npm audit" in workflow, "nothing scans the web tree for advisories"
    installs = [
        line
        for line in workflow.splitlines()
        if "npm ci" in line and not line.lstrip().startswith("#")
    ]
    assert installs, "CI no longer installs the web dependencies"
    for line in installs:
        assert "--no-audit" not in line, (
            f"`{line.strip()}` opts out of the one vulnerability report this pipeline "
            "gets for free"
        )
    assert "schedule:" in workflow and "cron:" in workflow, (
        "with no scheduled run, an advisory published between pushes is noticed "
        "whenever somebody next happens to push"
    )
    assert "no-cache: ${{ github.event_name == 'schedule' }}" in workflow, (
        "the scheduled run must bypass the buildx cache, or it republishes the "
        "same frozen layers and rebuilds nothing"
    )
    assert "target: runtime" in workflow, (
        "still the shipped stage — the dev stages carry pytest and the whole tree"
    )


def test_dependabot_watches_all_three_ecosystems() -> None:
    config = yaml.safe_load(DEPENDABOT.read_text())
    ecosystems = {entry["package-ecosystem"] for entry in config["updates"]}
    assert {"npm", "github-actions", "pip"} <= ecosystems, (
        f"dependabot.yml covers only {sorted(ecosystems)}. github-actions matters as "
        "much as the others: those actions run with `packages: write` against ghcr"
    )


def _healthcheck_command(manifest: Path, service: str) -> str:
    services = yaml.safe_load(manifest.read_text())["services"]
    healthcheck = services[service].get("healthcheck")
    assert healthcheck, (
        f"{manifest.name}: `{service}` has no healthcheck. A wedged worker and an idle "
        "worker both report `Up` with no logs — this is the process that does the "
        "work, and invariant 8 is that nothing fails silently"
    )
    test = healthcheck["test"]
    return " ".join(test) if isinstance(test, list) else str(test)


def test_the_worker_healthcheck_watches_the_heartbeat_the_worker_writes() -> None:
    """A healthcheck for a *process* would pass on a hung worker; this one cannot.

    `runner._heartbeat` stamps the file from the same event loop the pipeline
    runs on, so a loop that has stopped turning stops stamping it.
    """
    for manifest in (COMPOSE, ZIMAOS):
        command = _healthcheck_command(manifest, "worker")
        assert str(runner.HEARTBEAT_PATH) in command, (
            f"{manifest.name}: the worker healthcheck does not read "
            f"{runner.HEARTBEAT_PATH} — check whatever it does read still proves the "
            "event loop is turning, rather than that a process exists"
        )
        threshold = re.search(r"<\s*(\d+(?:\.\d+)?)", command)
        assert threshold, f"{manifest.name}: no staleness threshold found in {command!r}"
        assert float(threshold.group(1)) == runner.HEARTBEAT_STALE_SECONDS, (
            f"{manifest.name}: the manifest calls the heartbeat stale at "
            f"{threshold.group(1)}s while runner.HEARTBEAT_STALE_SECONDS is "
            f"{runner.HEARTBEAT_STALE_SECONDS}s. Two numbers that must agree and are "
            "written down twice; keep them in step"
        )


def test_the_heartbeat_is_written_far_more_often_than_it_is_called_stale() -> None:
    """A healthcheck that flaps is one that gets switched off.

    The heavy work is offloaded (`asyncio.to_thread`, `create_subprocess_exec`),
    but a GIL-bound stretch inside a thread can still starve the loop for
    seconds, so the margin has to absorb several missed stamps.
    """
    assert runner.HEARTBEAT_STALE_SECONDS >= 4 * runner.HEARTBEAT_INTERVAL_SECONDS, (
        f"stale at {runner.HEARTBEAT_STALE_SECONDS}s is too tight for a stamp every "
        f"{runner.HEARTBEAT_INTERVAL_SECONDS}s"
    )


def test_the_worker_runs_the_heartbeat_task() -> None:
    """The manifests are half of it; a healthcheck with nothing writing the file
    would fail every worker on every deploy."""
    source = (ROOT / "worker" / "runner.py").read_text()
    assert 'name="heartbeat"' in source, (
        "runner.main() no longer starts the heartbeat task, so the file the "
        "healthcheck reads is never written and the worker is permanently unhealthy"
    )
