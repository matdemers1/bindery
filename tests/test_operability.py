"""What is true of the deployment, asserted rather than remembered.

Every claim here was a defect first. They share a shape: the code was right, the
thing *around* the code was wrong, and nothing in a pipeline that builds three
images and drives a browser would have noticed — because none of it is reachable
from a request.

- A `uses:` on a floating major tag, in the one job holding `packages: write`
  against ghcr.
- An import of a dev-extra package in production code: green in every gate,
  `ModuleNotFoundError` in the deployed image.
- `cloudflare/cloudflared:latest` terminating the household's only ingress,
  beside four services with deliberate tags.
- Both shipped images running as uid 0, which makes the `0444` on every original
  advisory rather than enforced.
- No log rotation, on a box where the Docker data root shares a device with
  `pgdata`.
- A 20-second graceful shutdown behind Docker's 10-second SIGKILL, so the block
  that hands claims back never ran.
- nginx resolving `api` once at startup, so recreating the api alone 502s
  everything while `docker ps` shows five healthy containers.
- Runbooks describing a perimeter that was removed and an offsite copy that was
  built.

Read as file contents, like `test_supply_chain.py`: cheap, no stack, and it runs
in the default suite.
"""

import ast
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from worker import runner

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "infra" / "docker-compose.yml"
ZIMAOS = ROOT / "infra" / "zimaos" / "bindery.zimaos.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"
NGINX = ROOT / "infra" / "nginx.conf"
BACKUP_CRON = ROOT / "infra" / "zimaos" / "bindery-backup.cron"
DEPLOY_DOC = ROOT / "docs" / "zimaos-deploy.md"
BACKUP_DOC = ROOT / "docs" / "backup-and-restore.md"
ACCESS_DOC = ROOT / "docs" / "access-setup.md"

# Everything that stays running and therefore keeps writing to a log file. The
# `test`, `test-worker`, `e2e` and `screenshots` services are `run --rm` and
# exit, so an unbounded log is not a thing they can have.
LONG_RUNNING = ["postgres", "api", "worker", "web"]

# Where Docker's default ten seconds is demonstrably too short. `web` is not
# here on purpose: nginx drains in well under a second and giving it a longer
# grace period would only slow every deploy down for nothing.
NEEDS_A_GRACE_PERIOD = ["postgres", "api", "worker"]


def _services(manifest: Path) -> dict:
    return yaml.safe_load(manifest.read_text())["services"]


def _seconds(value: str | int) -> float:
    """`30s`, `2m`, or a bare number of seconds."""
    text = str(value).strip()
    if text.endswith("ms"):
        return float(text[:-2]) / 1000
    if text.endswith("s"):
        return float(text[:-1])
    if text.endswith("m"):
        return float(text[:-1]) * 60
    return float(text)


# ---------------------------------------------------------------------------
# CI's own supply chain
# ---------------------------------------------------------------------------

def test_every_action_is_pinned_to_a_commit_sha() -> None:
    """`v5` is a branch-like ref its owner can repoint at any commit.

    The `images` job holds `packages: write`, logs in to ghcr with
    `secrets.GITHUB_TOKEN`, and hands `docker/build-push-action` the credentials
    and the push. A rewritten tag on any action in that job is attacker
    controlled code inside a runner that can publish the images this household's
    archive pulls, without anybody reviewing a line of Bindery's source — which
    is not hypothetical, it is what happened to tj-actions. `dependabot.yml`
    keeps the SHAs current, so this costs nothing to hold.
    """
    floating = []
    for number, line in enumerate(WORKFLOW.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("- uses:") and not stripped.startswith("uses:"):
            continue
        ref = stripped.split("uses:", 1)[1].strip().split("#")[0].strip()
        _, _, version = ref.partition("@")
        if not re.fullmatch(r"[0-9a-f]{40}", version):
            floating.append(f"{number}: {ref}")
    assert not floating, (
        "these actions are named by a mutable ref rather than a commit SHA:\n  "
        + "\n  ".join(floating)
        + "\nPin as `owner/action@<40-hex sha>  # vX.Y.Z`. Dependabot rewrites both "
        "halves on its weekly run, so the pin does not go stale."
    )


def test_no_image_tracks_a_floating_latest_tag() -> None:
    """`:latest` is a tag that changes underneath a `docker compose pull`.

    cloudflared is the one that mattered: it runs `--no-autoupdate`, so the tag
    is its only update path, and it is the process every request to the archive
    passes through (ADR-006, REQ-104). On `:latest` an unrelated maintenance
    pull swapped it for whatever Cloudflare published since, with no version
    recorded anywhere and nothing to roll back to.

    `:main` is allowed and is not an oversight — CLAUDE.md documents it as the
    deploy channel, with `:sha-` tags for rollback, and it moves only when this
    repository's own pipeline publishes it.
    """
    for manifest in (COMPOSE, ZIMAOS):
        for name, service in _services(manifest).items():
            image = service.get("image")
            if not image:
                continue  # built from a Dockerfile in this repo
            assert ":" in image.rsplit("/", 1)[-1], (
                f"{manifest.name}: `{name}` names {image} with no tag, which means "
                "`:latest`"
            )
            assert not image.endswith(":latest"), (
                f"{manifest.name}: `{name}` tracks {image}. Pin a released version: "
                "on `:latest` the next `docker compose pull` replaces it with "
                "something nobody chose and nothing records what was there before"
            )


# ---------------------------------------------------------------------------
# What the shipped images actually contain, and who runs it
# ---------------------------------------------------------------------------

def _third_party_imports(package: str) -> dict[str, set[str]]:
    """Top-level module names `package` imports, and where from."""
    found: dict[str, set[str]] = {}
    first_party = {"api", "worker", "tests", "alembic", "__future__"}
    for path in sorted((ROOT / package).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in sys.stdlib_module_names or top in first_party:
                    continue
                found.setdefault(top, set()).add(str(path.relative_to(ROOT)))
    return found


def _declared() -> tuple[list[Requirement], list[Requirement], list[Requirement]]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    return (
        [Requirement(text) for text in project["dependencies"]],
        [Requirement(text) for text in extras["worker"]],
        [Requirement(text) for text in extras["dev"]],
    )


def _closure(requirements: list[Requirement]) -> set[str]:
    """Canonical names of everything installing `requirements` would bring in.

    Walked from installed metadata, honouring the extras the requirement asks
    for — `uvicorn[standard]` pulls in httptools and uvloop, and pretending
    otherwise would flag them as undeclared. A distribution that is not
    installed in *this* image contributes only its own name: the worker extra is
    absent from the api test image, which is exactly why the assertions below
    fall back to the declared names for it rather than demanding a closure they
    cannot compute.
    """
    reached: set[str] = set()
    pending = [(canonicalize_name(r.name), frozenset(r.extras)) for r in requirements]
    seen: set[tuple[str, frozenset[str]]] = set()
    while pending:
        name, extras = pending.pop()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        reached.add(name)
        try:
            requires = distribution(name).requires or []
        except PackageNotFoundError:
            continue
        for raw in requires:
            dependency = Requirement(raw)
            if dependency.marker is None:
                wanted = True
            else:
                wanted = dependency.marker.evaluate({"extra": ""}) or any(
                    dependency.marker.evaluate({"extra": extra}) for extra in extras
                )
            if wanted:
                pending.append(
                    (canonicalize_name(dependency.name), frozenset(dependency.extras))
                )
    return reached


def test_production_code_never_imports_a_dev_only_package() -> None:
    """The axis `test_layering.py`'s name claims and its assertions do not cover.

    That module guards `api` importing `worker`, and asserts the api runtime
    stage copies `api/` and not `worker/`. Despite its name, the second check
    reads source *directories*; it says nothing about distributions. Both dev
    stages install base + the `dev` extra, so `import httpx` in production code
    resolves in `make test`, in `make test-pipeline` and in both CI pytest
    gates, and raises `ModuleNotFoundError` in the images that ship — where
    httpx, python-docx and openpyxl are all absent (verified against the built
    images, not assumed).

    httpx is the one to expect: it is the obvious import for anyone adding an
    outbound HTTP call — a webhook, an offsite probe, a health check — and it is
    in the dev extra. The failure lands at request time on the deployed host
    after a fully green five-gate pipeline, which is the failure this project
    has already paid for once.
    """
    base, worker_extra, dev = _declared()
    production = _closure(base) | {canonicalize_name(r.name) for r in worker_extra}
    dev_only = _closure(dev) - production
    provided = packages_distributions()
    forbidden = {
        module: sorted(dev_only.intersection(canonicalize_name(d) for d in dists))
        for module, dists in provided.items()
        if dev_only.intersection(canonicalize_name(d) for d in dists)
    }

    offences = []
    for package in ("api", "worker"):
        for module, files in sorted(_third_party_imports(package).items()):
            if module in forbidden:
                offences.append(
                    f"{module} (from {', '.join(forbidden[module])}) in "
                    + ", ".join(sorted(files))
                )
    assert not offences, (
        "production code imports packages that exist only in the `dev` extra, so "
        "this is a ModuleNotFoundError in the deployed image on a path every CI "
        "gate runs green:\n  " + "\n  ".join(offences) + "\nMove the dependency "
        "into `dependencies` (or the `worker` extra) in pyproject.toml and run "
        "`make lock`, or do not import it."
    )


def test_every_third_party_import_is_something_its_image_would_carry() -> None:
    """The general form: `api/` gets the base closure, `worker/` gets base + its extra.

    Transitive is fine and deliberate — `botocore` arrives with boto3 and `PIL`
    with pikepdf, and both are genuinely present in the runtime images. What is
    not fine is a name that reaches neither list, which is a package that
    happened to be installed in whichever image the test ran in.
    """
    base, worker_extra, _ = _declared()
    api_allowed = _closure(base)
    worker_allowed = api_allowed | {canonicalize_name(r.name) for r in worker_extra}
    declared_names = {
        "api": {canonicalize_name(r.name) for r in base},
        "worker": {canonicalize_name(r.name) for r in base + worker_extra},
    }
    allowed = {"api": api_allowed, "worker": worker_allowed}
    provided = packages_distributions()

    offences = []
    for package in ("api", "worker"):
        for module, files in sorted(_third_party_imports(package).items()):
            dists = {canonicalize_name(d) for d in provided.get(module, [])}
            if dists:
                ok = bool(dists & allowed[package])
            else:
                # Not installed in this image at all. The only legitimate case is
                # the worker extra, absent from the api test image — so fall back
                # to what pyproject declares rather than to whatever is here.
                ok = canonicalize_name(module.replace("_", "-")) in declared_names[package]
            if not ok:
                came_from = ", ".join(sorted(dists)) or "not installed here"
                offences.append(
                    f"{package}/ imports {module} ({came_from})"
                    f" — e.g. {sorted(files)[0]}"
                )
    assert not offences, (
        "these imports are not reachable from what pyproject.toml declares for "
        "that image:\n  " + "\n  ".join(offences) + "\nDeclare the dependency and "
        "run `make lock`."
    )


def _stage(dockerfile: Path, name: str) -> str:
    text = dockerfile.read_text()
    marker = f"AS {name}"
    assert marker in text, f"{dockerfile.name} has no `{marker}` stage"
    body = text.split(marker, 1)[1]
    return body.split("\nFROM ", 1)[0]


def test_the_shipped_images_run_unprivileged() -> None:
    """0444 on an original is only a guarantee if the process cannot ignore it.

    Invariant 1 — originals are never modified — is enforced in this deployment
    by file mode alone, and a process running as uid 0 ignores the write bit. The
    whole archive is bind-mounted read-write at /data, under a login page that
    has faced the open internet since Cloudflare Access was removed (invariant
    9), so a write bug, a traversal in an upload filename or a compromise of the
    api landed as root on the thing the product *is*.

    `dev` deliberately stays root: the test container bind-mounts the working
    tree from whatever uid the developer's checkout happens to use.

    The host has to agree — `/data` owned by the same uid — which is why
    docs/zimaos-deploy.md step 3 and the e2e job both chown it.
    """
    for name in ("api", "worker"):
        dockerfile = ROOT / "infra" / f"Dockerfile.{name}"
        runtime = _stage(dockerfile, "runtime")
        users = re.findall(r"^USER\s+(\S+)", runtime, re.MULTILINE)
        assert users, (
            f"{dockerfile.name}: the `runtime` stage declares no USER, so the "
            "shipped image runs as root and the 0444 on every original is "
            "advisory"
        )
        assert users[-1] not in ("root", "0"), (
            f"{dockerfile.name}: the `runtime` stage runs as {users[-1]}"
        )
        assert "USER" not in _stage(dockerfile, "dev"), (
            f"{dockerfile.name}: the `dev` stage should stay root — it bind-mounts "
            "the working tree, and a uid that does not match the checkout makes "
            "`make test` fail on file permissions rather than on code"
        )

    chowns = [
        line
        for line in WORKFLOW.read_text().splitlines()
        if "chown" in line and "bindery-ci-data" in line
    ]
    assert chowns, (
        "the e2e job creates /tmp/bindery-ci-data as the runner and then starts "
        "containers that are no longer root, so without a chown they come up "
        "healthy and fail on the first write — the same failure the deploy "
        "runbook's step 3 exists to prevent on the real host"
    )


# ---------------------------------------------------------------------------
# What the manifests promise the host
# ---------------------------------------------------------------------------

def test_every_long_running_service_caps_its_container_logs() -> None:
    """Docker's json-file driver keeps everything until the disk is full.

    On this host the Docker data root is on `/DATA` — the same 904 GB
    non-redundant NVMe as `pgdata`. So uvicorn's access log and the worker's
    per-stage lines eventually stop Postgres being able to write, and an archive
    holding documents that cannot be reissued presents at 3am as a database that
    will not start with an obscure disk error. `event_log` is the record that is
    deliberately never pruned; this is the same text with none of the value.
    """
    for manifest in (COMPOSE, ZIMAOS):
        services = _services(manifest)
        for name in LONG_RUNNING + (["cloudflared"] if "cloudflared" in services else []):
            logging = services[name].get("logging")
            assert logging, (
                f"{manifest.name}: `{name}` has no `logging:` block, so it writes "
                "into an unbounded json-file log on the device holding the database"
            )
            options = logging.get("options") or {}
            assert options.get("max-size"), f"{manifest.name}: `{name}` sets no max-size"
            assert int(options.get("max-file", 0)) >= 1, (
                f"{manifest.name}: `{name}` sets no max-file, so max-size caps one "
                "file and nothing caps the number of them"
            )


def test_the_stop_grace_periods_outlast_the_shutdown_they_exist_for() -> None:
    """20 seconds of graceful shutdown behind a 10-second SIGKILL is 0 seconds.

    `runner.main()` waits `SHUTDOWN_GRACE_SECONDS` for in-flight work and then
    releases any remaining claims, so "a restart picks it up now rather than
    after the lease expires". Docker's default stop timeout is 10s, so SIGKILL
    arrived while the worker was still inside `asyncio.wait` and that release
    never ran: every deploy stranded whatever was in flight until its per-stage
    lease expired — up to 45 minutes for normalize, at which point the health
    panel raised a critical `stuck` alert and paged the operator about a deploy
    they had performed on purpose. queue.py records the same symptom happening
    twice in one evening.
    """
    for manifest in (COMPOSE, ZIMAOS):
        services = _services(manifest)
        for name in NEEDS_A_GRACE_PERIOD:
            grace = services[name].get("stop_grace_period")
            assert grace, (
                f"{manifest.name}: `{name}` has no `stop_grace_period`, so Docker "
                "SIGKILLs it ten seconds after SIGTERM"
            )
        worker = _seconds(services["worker"]["stop_grace_period"])
        assert worker > runner.SHUTDOWN_GRACE_SECONDS, (
            f"{manifest.name}: the worker is killed after {worker}s while "
            f"runner.SHUTDOWN_GRACE_SECONDS is {runner.SHUTDOWN_GRACE_SECONDS}s — "
            "the block that hands its claims back is unreachable. Raise the grace "
            "period or lower the constant; they must not disagree"
        )


def test_no_service_publishes_a_host_port() -> None:
    """REQ-104, invariant 9. Ingress is the Cloudflare Tunnel and nothing else.

    Since Access was removed the login page faces the open internet, so a
    published port is not a smaller version of the same exposure — it is a
    second front door onto the LAN with nothing in front of it at all.
    """
    for manifest in (COMPOSE, ZIMAOS):
        published = [
            name for name, service in _services(manifest).items() if service.get("ports")
        ]
        assert not published, (
            f"{manifest.name}: {', '.join(published)} publish host ports. "
            "Ingress is the tunnel only (REQ-104, ADR-006)"
        )


def test_nginx_re_resolves_the_api_container_rather_than_caching_it_forever() -> None:
    """Recreating the api alone must not 502 the whole app.

    With a literal hostname and no `resolver`, nginx resolves `api` at
    configuration load and caches that address for the life of the process —
    and Docker hands a container a new IP whenever it is *recreated*. Rolling
    back one service to a `:sha-` tag, or `up -d api` after an api-only change,
    then left web proxying to an address nothing answers on: every `/api/`
    request 502s while `docker ps` shows all five containers `Up` and web's own
    healthcheck — which only fetches the static index — still passing. Routine
    full deploys hide it, because CI republishes all three images every commit.

    The variable is what forces re-resolution, and it has to carry
    `$request_uri`: any variable in `proxy_pass` switches nginx out of
    prefix-replacement mode, so a literal `/api/` there would forward every
    request as `/api/` with no path and no query string.
    """
    conf = NGINX.read_text()
    assert "resolver 127.0.0.11" in conf, (
        "nginx.conf declares no resolver, so the api's address is resolved once at "
        "startup and cached for the life of the worker process"
    )
    proxy = next(
        line.strip() for line in conf.splitlines() if line.strip().startswith("proxy_pass")
    )
    assert "$" in proxy, (
        f"`{proxy}` names a literal upstream, which nginx resolves once. Use a "
        "variable so it re-resolves against Docker's embedded DNS"
    )
    assert "$request_uri" in proxy, (
        f"`{proxy}` uses a variable upstream with a literal URI part. nginx then "
        "sends that URI verbatim and drops the request's own path and query "
        "string — every route would arrive at the api as `/api/`"
    )


def test_the_api_can_tell_it_is_behind_tls() -> None:
    """Three pieces have to agree, and any one of them alone does nothing.

    TLS is terminated at Cloudflare; the tunnel speaks plain http to nginx, and nginx to the api.
    So `request.url.scheme` inside the app is `http` unless the browser's scheme is forwarded
    *and* uvicorn is willing to believe it — uvicorn trusts only 127.0.0.1 by default, and nginx
    is a different container whose address Docker reassigns on every recreate.

    Missed, this is invisible until something builds an absolute URL. The first real
    *Sign in with D3 Auth* attempt sent `http://bindery.d3cloud.io/api/auth/oidc/callback` to a
    provider holding the https form, and was refused with `invalid_redirect_uri` — a redirect URI
    is compared character for character, by design, because that comparison is what stops an
    authorization code being delivered somewhere else.
    """
    conf = NGINX.read_text()
    assert "X-Forwarded-Proto" in conf, (
        "nginx forwards no scheme, so every absolute URL the api builds claims http"
    )

    # The *serving* CMD, not the last one in the file: the `dev` stage ends with
    # `CMD ["python", "-m", "pytest"]`, and reading that would pass this test while the shipped
    # image still built http:// URLs. Continuations are folded first, so a CMD split over two
    # lines is one string.
    dockerfile = (ROOT / "infra" / "Dockerfile.api").read_text().replace("\\\n", " ")
    command = next(
        (line for line in dockerfile.splitlines()
         if line.startswith("CMD ") and "uvicorn" in line),
        None,
    )
    assert command, "the api image no longer starts uvicorn from a CMD; this guard needs rewriting"
    assert "--proxy-headers" in command, (
        "uvicorn is started without --proxy-headers, so X-Forwarded-Proto is ignored"
    )
    assert "--forwarded-allow-ips" in command, (
        "uvicorn trusts 127.0.0.1 by default and nginx is another container, so the header "
        "arrives from an address it will not believe"
    )


# ---------------------------------------------------------------------------
# The runbooks, which are read at 3am and are therefore part of the system
# ---------------------------------------------------------------------------

def test_the_nightly_backup_is_in_the_repository_and_runs_the_supported_path() -> None:
    """A backup only the drill cannot verify is a backup nobody has restored.

    The host-only `backup.sh` this replaced dumped Postgres and rsynced blobs
    into flat files. `scripts/restore-drill.sh` — "the deliverable, not the
    backup" — requires a *generation*: `bindery.dump`, `blobs/`, `vault/` and
    `manifest.json` in one directory. So the backups that ran every night were
    the ones the drill could not consume, and the ones it could only existed
    when a human remembered to type `make backup`. The vault is the sharpest
    edge: a sealed object has no second source and lives outside `blobs/`, so a
    plain rsync never saw it.
    """
    cron = BACKUP_CRON.read_text()
    assert "api.export.cli backup" in cron, (
        "the scheduled backup does not invoke the supported path, so whatever it "
        "writes is not a generation the restore drill can be pointed at"
    )
    assert re.search(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S", cron, re.MULTILINE), (
        f"{BACKUP_CRON.name} contains no crontab line"
    )
    # Invariant 3. A scheduled prune of the backup tree is an unattended
    # destructive path on the one tree that exists to survive a mistake.
    for destructive in ("rm -rf", "find ", "-delete", "-mtime"):
        assert destructive not in cron.split("30 3 * * *")[-1], (
            f"the scheduled command contains `{destructive}`. Retention here is "
            "manual on purpose: invariant 3 is that nothing is ever automatically "
            "deleted and no unattended destructive code path may exist"
        )


def test_the_runbooks_do_not_send_the_operator_to_configure_a_removed_perimeter() -> None:
    """This is the file a host rebuild is performed from.

    It walked the operator through creating the Access application, the Allow
    policy and a service token, and told them to expect an Access challenge in
    the browser — all of it removed by ADR-008 ten months of decisions ago, with
    the correction buried 130 lines below the instructions it invalidated.
    Following it end to end reinstates a gate that was deliberately taken away,
    spends time on a service token that authenticates nothing, and — because the
    reader has been told to expect a challenge — makes "the login page is
    directly exposed" look like a success rather than the state that makes
    `api/auth/throttle.py` load-bearing.
    """
    for doc in (DEPLOY_DOC, ACCESS_DOC):
        text = doc.read_text()
        for instruction in (
            "Create Service Token",
            "Add an application",
            "CF-Access-Client-Secret: <secret>",
        ):
            assert instruction not in text, (
                f"{doc.name} still tells the operator to set up Cloudflare Access "
                f"({instruction!r}). It was removed on 2026-08-30 (ADR-008) and "
                "reinstating it breaks scoped API tokens"
            )
        assert "ADR-008" in text, (
            f"{doc.name} describes the ingress path without naming the decision "
            "that removed Access, so the next reader has no way to know"
        )

    head = DEPLOY_DOC.read_text().split("## 1.")[0]
    assert "ADR-008" in head, (
        "the Access-was-removed warning has to be above the instructions it "
        "qualifies, not below them — that was the original defect"
    )
    assert "There is no verified backup yet" not in DEPLOY_DOC.read_text(), (
        "section 0 still says backup and the restore drill are unbuilt. Phase 6 "
        "and Phase 13 both shipped, and the drill runs in CI on every commit"
    )


def test_the_backup_runbook_describes_the_offsite_copy_that_exists() -> None:
    """An operator following it after a fire would conclude the archive is gone.

    The 3-2-1 table said copy 3 was "Not built yet", in bold, and that "both
    existing copies are in the same building on the same array". Phase 13 built
    it: api/offsite.py replicates to S3 under a KMS key on a cadence the worker
    drives, and `make drill-offsite` restores from the bucket alone. Drift in a
    runbook is an outage-lengthening defect, not untidiness.
    """
    text = BACKUP_DOC.read_text()
    assert "Not built yet" not in text and "Copy 3 does not exist yet" not in text, (
        "docs/backup-and-restore.md still says the offsite copy is unbuilt, so the "
        "one procedure that recovers the archive after a fire — "
        "`make drill-offsite` — is documented as pending"
    )
    for present in ("drill-offsite", "offsite-replication.md", "lifecycle-check"):
        assert present in text, f"the 3-2-1 section never mentions {present}"


def test_the_deploy_runbook_covers_the_migration_that_fails_partway() -> None:
    """The composite 3am state, which the one-sentence rollback did not reach.

    New images running, 0025 and 0026 applied, 0027 failed, and the operator
    pins `:sha-` back to yesterday's build — which now runs against a schema
    ahead of it. `api/version.py` detects that direction and reports it; the
    runbook offered no next move. The pre-migration dump was a blockquote at the
    end of the section rather than a numbered command, which made it the step
    most likely to be skipped by somebody moving quickly.
    """
    text = DEPLOY_DOC.read_text()
    for move in ("alembic current", "alembic downgrade", "alembic history"):
        assert move in text, (
            f"the deploy runbook never mentions `{move}`, so there is no way to "
            "tell where a partly-applied upgrade stopped or how to get back"
        )
    sequence = text.split("## Deploying a change afterwards")[1].split("### If the")[0]
    assert "api.export.cli backup" in sequence, (
        "the pre-migration dump is not a command inside the deploy sequence. R-10 "
        "is a bad migration corrupting the archive and this is the whole "
        "mitigation — it cannot be an aside after the commands"
    )
    assert "docker-compose.staging.yml" in text, (
        "nothing points at the staging stack, which was written for exactly this "
        "and is the only way to find out before rather than during"
    )
