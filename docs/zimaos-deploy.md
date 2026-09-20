# Deploying Bindery to the ZimaOS host

Everything here is done once. After that a deploy is `docker compose pull &&
docker compose up -d`, and you choose when it happens — nothing auto-updates the
thing holding your passport.

**Host:** ZimaOS at `<zima-lan-ip>`
**Hostname:** `bindery.d3cloud.io` (Cloudflare Tunnel — no ports opened on the Zima)

> [!warning] There is no Cloudflare Access in front of this hostname
> It was removed on 2026-08-30 (**ADR-008**), and this document used to walk you
> through setting it up. It no longer does. The tunnel still carries every
> request and there are still no published host ports (REQ-104) — only the
> Access *policy* went away. `bindery.d3cloud.io` answers the open internet with
> Bindery's own login page, which is why `api/auth/throttle.py`, the password
> policy and the nginx security headers are load-bearing rather than defence in
> depth. **Do not reinstate Access**; it broke scoped API tokens (R-15) and that
> is what the removal was for. The service token in `.deploy/cloudflare.json` is
> dead — programmatic access uses Bindery's own scoped API tokens (REQ-107).
> Section 5 records what was there, for anyone reading an old screenshot.

---

## 0. Before you start — read this part

- **The backups are built, and one of them is proven.** Copy 2 is a nightly
  generation from `infra/zimaos/bindery-backup.cron` (step 9); copy 3 is the
  offsite replication to S3 that the worker runs on its own cadence
  (`docs/offsite-replication.md`). The restore drill runs in CI on every commit
  to `main`, against a real dump, and a negative case proves it can still fail.
  What is *not* automatic is running the drill against **this host's** data:
  `make drill b=<generation>` and `make drill-offsite` are the two commands that
  turn "there are backups" into "I restored one".
- **OCR accuracy on your real documents has never been measured** (T-1.12, the
  R-01 gate). Until it is, treat classification output as unproven. This is the
  one item from the original version of this list that is still open.

---

## 1. ZimaOS's two quirks — read these first

Both cost real time to discover. Everything else follows from them.

**`/` is a read-only squashfs.** The OS image is immutable. `/root` cannot be
written to *even as root*. `/etc` and `/DATA` are writable overlays.

**`HOME=/DATA` for your shell, `HOME=/root` for services.** So anything you
write to `~` lands in `/DATA`, while sshd and the Docker daemon look in `/root`
and find nothing. This produces two failures that look unrelated:

| Symptom | Cause | Fix |
|---|---|---|
| SSH key rejected despite correct perms | key went to `/DATA/.ssh`, sshd reads `/root/.ssh` | `AuthorizedKeysFile /DATA/.ssh/authorized_keys` in `sshd_config` |
| "Failed to pull image after trying 5 mirror methods" | `docker login` wrote `/DATA/.docker/config.json`, the daemon reads `/root/.docker/` | `export DOCKER_CONFIG=/DATA/.docker` before any pull |

The mirror-hunting error is especially misleading: it reads like a network or
registry problem and is actually a credentials-path problem.

Also: `PermitRootLogin` ships as `no`. For key-based root access:

```bash
sed -i 's/^\s*PermitRootLogin\s\+no\s*$/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sshd -t && systemctl restart sshd
```

`sshd -T` reads the *file*; it does not prove the running daemon reloaded. Check
`ps -o pid,lstart -C sshd` for a fresh start time.

---

## 2. A GitHub token the Zima can pull with

The repo is private, so the images are private, and an anonymous pull returns a
confusing "not found" rather than "unauthorized".

Create a **classic** personal access token with the single scope
`read:packages` at <https://github.com/settings/tokens>. Fine-grained tokens do
not currently work for GHCR pulls.

Then, on the Zima — over SSH (`ssh root@<zima-lan-ip>`) or the terminal in the
ZimaOS web UI:

```bash
echo "<the-token>" | docker login ghcr.io -u matdemers1 --password-stdin
```

That writes `/DATA/.docker/config.json`. **Every subsequent docker command that
needs to pull must be run with `DOCKER_CONFIG=/DATA/.docker`** — see the quirks
above. This is also why the CasaOS custom-app installer cannot pull these
images: it has no way to set that variable.

---

## 3. Storage — `/DATA` is *not* the pool

This is the trap. `/DATA` is a **904 GB NVMe partition**. The 16 TB array is
mounted elsewhere:

| Mount | Size | What it is |
|---|---|---|
| `/media/Main-Storage` | 10.9 TB | btrfs on **md RAID 5**, four disks, survives one failure |
| `/DATA` | 904 GB | single NVMe partition, no redundancy |

The archive is split deliberately:

```bash
mkdir -p /media/Main-Storage/bindery/data/inbox/household   # blobs, derived, inbox
mkdir -p /DATA/AppData/bindery/pgdata                        # database

# REQUIRED. The api and worker images run as uid 10001, not root (see
# infra/Dockerfile.api). Skip this and both containers come up *healthy* and
# then fail on the first write — an ingest that never finishes, with a
# permission error several layers down. Postgres manages its own directory's
# ownership, so pgdata is deliberately not in this command.
chown -R 10001:10001 /media/Main-Storage/bindery/data
```

They run unprivileged because in this deployment invariant 1 — originals are
never modified — is enforced by file mode: every blob is written `0444`. A
process running as uid 0 ignores the write bit entirely, so as root that
invariant was a convention rather than a guarantee, on a login page facing the
open internet. Now a stray write, or a traversal in an upload filename, is a
permission error against a read-only file instead of an overwritten original.

`web` is still root inside its container, because nginx binds port 80 and the
tunnel's public hostname points at `web:80`; changing that is an ingress change,
not a Dockerfile one. It holds no volumes and serves static files.

**Blobs go on the array** because they are the irreplaceable bytes and RAID 5
turns a disk failure into an inconvenience (R-09). **Postgres goes on NVMe** for
IOPS — it is not redundant, but it is fully reconstructible from the nightly
`pg_dump`, which lands on the array.

Putting everything on `/DATA` would leave every original on one disk.

The inbox subdirectory name must match a library name — `household` matches a
library called "Household". That is how a scanner picks a destination.

---

## 4. Cloudflare Tunnel

The tunnel is what makes the archive reachable without opening a port. Create it
at **Zero Trust → Networks → Tunnels → Create a tunnel**, choose *Cloudflared*,
name it `bindery`, and copy the token it shows you.

Add one public hostname to the tunnel:

| Field | Value |
|---|---|
| Subdomain | `bindery` |
| Domain | `d3cloud.io` |
| Service | `HTTP` → `web:80` |

`web:80` is the nginx container, which serves the frontend and proxies `/api/`
to the api container. One route covers both.

> DNS is created for you by the tunnel. Do not add an A record pointing at the
> Zima — there is nothing listening on a public port, and that is the point.

---

## 5. Access — there is none, and that is the current design

**Skip this section unless you are reading an old screenshot.** ADR-008 removed
Cloudflare Access from `bindery.d3cloud.io` on 2026-08-30. Nothing needs to be
configured in Zero Trust beyond the tunnel in step 4.

What that leaves in front of the archive, and what each piece is doing:

| Layer | What it stops |
|---|---|
| Cloudflare Tunnel (step 4) | Anything reaching the Zima except through Cloudflare. No host port is open (REQ-104, invariant 9) |
| `api/auth/throttle.py` | Password guessing. This is now the *primary* control, not a backstop |
| Bindery's own login + TOTP | Everyone else |
| The nginx security headers (`infra/nginx.conf`) | The things Access was incidentally standing in for — framing, sniffing, referrer leakage, and a CSP |
| Scoped API tokens (REQ-107) | Programmatic access, without a browser identity flow |

> **What used to be here.** An Access application on `bindery.d3cloud.io` with
> an Allow policy on one email address, plus a Service Auth policy and a service
> token so non-browser clients could get through. The service token path is what
> made it untenable: Access expects a browser identity flow, which breaks scoped
> API tokens and any future native client (risk R-15). The token in
> `.deploy/cloudflare.json` no longer authenticates anything. Do not recreate
> the application; a 302 to a Cloudflare login page is now a symptom, not a
> success.

---

## 6. Install

The CasaOS custom-app importer **cannot pull these images** — it has no way to
set `DOCKER_CONFIG`, so it falls back to mirror-hunting and fails. Deploy with
compose directly:

```bash
# copy the filled manifest to the host as /DATA/AppData/bindery/docker-compose.yml
cd /DATA/AppData/bindery
export DOCKER_CONFIG=/DATA/.docker
docker compose pull && docker compose up -d
```

The original CasaOS import route, kept for reference

**ZimaOS → Apps → Custom Install → Import**, and paste
`infra/zimaos/bindery.zimaos.yaml`.

Replace every `change-me` first:

| Value | How to produce it |
|---|---|
| `POSTGRES_PASSWORD` (twice — also inside `DATABASE_URL`) | `openssl rand -base64 32` |
| `JWT_SECRET` | `openssl rand -base64 48` |
| tunnel token in the `cloudflared` command | from step 3 |
| `ANTHROPIC_API_KEY` | leave empty unless you want classification now |

> **If the importer rejects the YAML**, the likely cause is the `&bindery-env` /
> `*bindery-env` anchor. Copy the `api` service's `environment:` block verbatim
> over `*bindery-env` in `worker`. They must stay identical — a worker with a
> different `DATABASE_URL` silently processes nothing.

Leaving `ANTHROPIC_API_KEY` empty is a supported state, not a broken one: the
archive ingests, OCRs, indexes, segments and searches, and only classification
defers (REQ-055).

---

## 7. First run

```bash
# Migrations are applied explicitly, never on container boot (REQ-114).
docker exec bindery-api alembic upgrade head

# The 14 known forms — DD-214, W-2, deed, title, passport…
docker exec bindery-api python -m api.cli seed-forms

# A fresh install is claimed in the browser with this code (Phase 19).
docker logs bindery-api 2>&1 | grep 'bindery setup'
```

Open the site and follow **Set up this archive**: the code above, the owner's
account, an authenticator, the recovery codes. That account ends as the
administrator. `docker exec bindery-api python -m api.cli setup-code` prints a
fresh code if the first one has expired or scrolled away.

---

## 8. Verify

```bash
# REQ-104: no service may publish a host port. No row may contain "->".
docker ps --filter name=bindery --format '{{.Names}}\t{{.Ports}}'

# All five up. postgres, api, worker and web report (healthy); cloudflared has
# no healthcheck of its own.
docker ps --filter name=bindery --format '{{.Names}}\t{{.Status}}'

# The pipeline is watching.
docker logs bindery-worker --tail 20   # expect "watching /data/inbox"
```

Then, from a browser: <https://bindery.d3cloud.io> should show Bindery's own
login page directly. **There is no Access challenge in front of it** — if you
get one, someone has re-created the Access application this deployment
deliberately removed (ADR-008), and scoped API tokens will have stopped
working.

**The real test** — drop a PDF into `/DATA/AppData/bindery/data/inbox/household/`
and watch it move:

```bash
docker logs -f bindery-worker
```

It should ingest (after the stability check), normalize, page, and segment. Then
press ⌘K in the browser and search for a word you know is inside it.

### When the worker goes unhealthy

The worker's healthcheck does not ask whether the process exists — a wedged
worker and an idle one both look like `Up` with no logs, which is precisely the
stall that never announces itself. It asks whether the event loop is still
turning: `worker/runner.py` stamps `/tmp/bindery-worker.heartbeat` every 15
seconds from the same loop the pipeline runs on, and the check fails once that
file is two minutes old.

**Docker will mark it and do nothing else.** `restart: unless-stopped` acts on
process *exit*; a plain Docker Engine healthcheck has no restart action at all.
So the container sits there saying `(unhealthy)` until somebody looks. Two ways
to stop that being a person's job, in order of preference:

```bash
# 1. Have the host restart anything unhealthy. Add to root's crontab on the
#    Zima. No extra container, and nothing gains access to the Docker socket
#    that does not already have it.
*/5 * * * * for c in $(docker ps --filter health=unhealthy --format '{{.Names}}'); do docker restart "$c"; done
```

```bash
# 2. Or just be told. The health panel already alerts on a stalled pipeline and
#    the notifier already has your webhook, but both of those live *inside* the
#    worker — so this is the one that still fires when the worker is the thing
#    that is stuck.
docker events --filter event=health_status
```

Option 1 is a deliberate choice over an autoheal sidecar
(`willfarrell/autoheal` and friends). Those work by bind-mounting
`/var/run/docker.sock`, which hands a third-party image tagged `latest` full
root over this host — on the box that holds the archive. A five-line cron entry
does the same job with nothing new to trust.

---

## 9. The nightly backup

Install the crontab entry that ships with the repository:

```bash
crontab -l > /tmp/crontab.now
cat infra/zimaos/bindery-backup.cron >> /tmp/crontab.now
crontab /tmp/crontab.now
crontab -l                       # both lines present?
```

It runs `docker exec bindery-api python -m api.export.cli backup` at 03:30 —
the same supported path as `make backup` and the Trust screen's button, not a
second implementation. Read the header of that file for what it does that a
`pg_dump` plus a blob rsync does not: the integrity check first, the sealed
vault objects, the pepper, and a `manifest.json` that makes each generation
self-describing.

**That last point is the reason this replaced a host-only `backup.sh`.** The
restore drill can only be pointed at a *generation* — a directory holding
`bindery.dump`, `blobs/`, `vault/` and `manifest.json`. A flat directory of
`.dump` files is not one, so the backups that ran every night were the ones the
drill could not verify, and the backups the drill could verify only existed when
a human remembered. They are now the same thing.

Prove one, rather than trusting the log:

```bash
generation=$(ls -1dt /media/Main-Storage/bindery/data/backups/*/ | head -1)
bash scripts/restore-drill.sh "$generation"
```

That restores into a throwaway Postgres, checks every original the restored
database references is present beside the dump, and searches the result for the
DD-214. It never touches the live stack. **Run it after every schema migration**
— and `make drill-offsite`, which does the same from S3 alone, at least
quarterly, because that is the only copy that survives the building.

`derived/` is deliberately not backed up — every byte of it is reproducible from
a blob by re-running the pipeline. Losing it costs CPU, not data.

> **Retention is manual, on purpose.** Each run writes a fresh full generation
> and nothing prunes them, because invariant 3 is that nothing is ever
> automatically deleted and no unattended destructive code path may exist —
> which is exactly what a scheduled prune of the backup tree would be. Watch
> `df -h /media/Main-Storage` and remove old generations by hand, keeping at
> least one you have actually drilled.

---

## Deploying a change afterwards

Push to `main`, wait for the build, then on the Zima. The commands are numbered
because the order is the whole procedure, and because the two that are easiest
to skip — the dump and `alembic current` — are the two you will want back.

> [!warning] The Zima's `docker-compose.yml` is its own file, and pulling images
> does not update it.
> `/DATA/AppData/bindery/docker-compose.yml` is a hand-maintained copy. It is
> *derived* from `infra/zimaos/bindery.zimaos.yaml`, not synchronised with it,
> so **a change to the compose in the repository does not reach this host at
> all** — `docker compose pull` fetches images, and nothing fetches the file
> that says how to run them.
>
> This is not hypothetical: the 2026-09-02 review added a worker healthcheck
> (CR-029), graceful-shutdown windows (CR-094) and log size caps (CR-095), all
> of them green in CI and none of them running in production until the host's
> own file was edited by hand three weeks later. The failure is silent in the
> worst way — the repository, the tests and the manifest all agree, and the box
> is the only thing that disagrees.
>
> So, before step 1: `git diff <last deployed sha>..HEAD -- infra/` and port
> anything that touched the compose or the manifest. Restart is required for it
> to take — `stop_grace_period`, `logging` and `healthcheck` only apply when a
> container is recreated. The `user:` change (CR-091) is the exception, because
> the uid is baked into the image by the Dockerfile and arrives with the pull.
>
> Checking what is actually running beats reading either file:
> ```bash
> docker inspect --format '{{.Config.User}} {{.Config.StopTimeout}} {{.HostConfig.LogConfig.Config}}' bindery-worker
> docker ps --filter name=bindery --format '{{.Names}}\t{{.Status}}'   # health, or its absence
> ```

```bash
cd /DATA/AppData/bindery
export DOCKER_CONFIG=/DATA/.docker            # or the pull is anonymous, and 401s

# 1. What is running now. Write both of these down; they are the rollback plan.
docker exec bindery-api alembic current                  # e.g. 0027_vault_pepper
docker ps --format '{{.Names}}\t{{.Image}}' --filter name=bindery

# 2. Does the incoming release add a migration? Ask before you deploy, not
#    after. This lists what the *new* image would apply on top of `current`.
docker compose pull
docker run --rm ghcr.io/matdemers1/bindery/api:main alembic heads
#    Same revision as step 1 -> no migration in this release; skip steps 3 and 6.

# 3. The dump. Not the nightly one — this one, taken now, immediately before
#    the schema changes. R-10 is a bad migration corrupting the archive and
#    this is the entire mitigation.
docker exec bindery-api python -m api.export.cli backup

# 4. New images.
docker compose up -d

# 5. Confirm the app is up on the old schema. `api/version.py` reports the drift
#    as "migration pending", which is the expected state between 4 and 6.
docker ps --filter name=bindery --format '{{.Names}}\t{{.Status}}'

# 6. Migrations, explicitly — never on container boot (REQ-114, invariant 10).
docker exec bindery-api alembic upgrade head
docker exec bindery-api alembic current                  # now the new revision
```

### If the migration fails partway

A multi-revision upgrade applies one revision at a time. If 0025 and 0026 land
and 0027 raises, the database is at 0026 and the images are running the code for
0027. `alembic current` is what tells you where you actually stopped — read it
before doing anything else.

```bash
docker exec bindery-api alembic current       # where did it stop?
docker exec bindery-api alembic history       # what was it trying to reach?
docker exec bindery-api alembic downgrade <the revision from step 1>
```

Every revision in this repository implements a real `downgrade`, or is listed in
`tests/test_migrations.py`'s `DELIBERATELY_IRREVERSIBLE` with the reason — a
test enforces that, so `downgrade` is a supported move rather than a hope. If
the downgrade also fails, stop and restore step 3's dump with
`scripts/restore-drill.sh` against a scratch container first, so you find out
whether it restores before you need it to.

**Rehearse instead of discovering.** `infra/docker-compose.staging.yml` exists
for exactly this: bring up a copy, restore last night's generation into it, and
run the upgrade there. A migration that fails on staging costs an evening; the
same migration on the live archive costs the dump you may or may not have taken.

### Rolling back

```bash
# In the compose file, change the three `:main` tags to `:sha-<commit>`, then:
docker compose up -d
```

> **Rolling the images back does not roll the schema back.** If the release you
> are backing out applied a migration, the database is still at the newer
> revision and yesterday's code is now running against a schema ahead of it.
> `api/version.py` detects that direction and the UI says so — but detecting it
> is not fixing it. Either `alembic downgrade <the revision from step 1>` as
> well, or restore step 3's dump. Decide which *before* you pin the old tag,
> because the app is serving requests either way.

### Bumping cloudflared

`cloudflared` is pinned to a released version in the manifest, like everything
else, and runs with `--no-autoupdate` — so the tag is the only update path and
nothing moves it on its own. It does need to stay reasonably current for tunnel
protocol compatibility, so bump it deliberately, roughly quarterly:

```bash
# Pick a version from https://github.com/cloudflare/cloudflared/releases,
# edit the `image:` line, then:
docker compose up -d cloudflared
curl -sS -o /dev/null -w '%{http_code}\n' https://bindery.d3cloud.io/api/health
```

A `200` means ingress survived it. If it did not, put the previous version back
— which you can only do because it was written down, which is why it is not
`:latest`.

### Two things that will waste your time if you guess them

- **The stack lives in `/DATA/AppData/bindery`**, not under `/var/lib/casaos/apps/`
  where every other CasaOS app is. `docker ps --format '{{.Label "com.docker.compose.project.config_files"}}'`
  tells you where any running stack's compose file actually is.
- **`DOCKER_CONFIG` must be set** for the pull. The registry credentials were
  written to `/DATA/.docker/config.json` (see the quirks table above); without
  the variable the client looks in `/root/.docker`, finds nothing, pulls
  anonymously, and gets `unauthorized` from ghcr.io — the packages are private,
  and they stay private.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `manifest unknown` / `not found` on pull | Step 1 not done, or the token lacks `read:packages` |
| A 502 on `/api/` while every container says `Up` and `web` says `(healthy)` | Almost always stale upstream resolution *if* you have just recreated the api alone. nginx caches `api`'s address; `infra/nginx.conf` sets `resolver 127.0.0.11 valid=10s` and puts a variable in `proxy_pass` so it re-resolves, but a `web` image older than that fix does not. `docker restart bindery-web` clears it in one move, and `docker compose up -d --force-recreate web` fixes it permanently. Note that web's own healthcheck only fetches the static index, so it passes throughout |
| A 502 on *everything*, including the login page | `web` is genuinely down, or the tunnel's public hostname points somewhere other than `web:80` |
| Files sit in the inbox untouched | The subdirectory name does not match a library name; check `docker logs bindery-worker` |
| Jobs queued but never claimed | Worker cannot reach the database — compare `DATABASE_URL` between the api and worker blocks |
| `bindery-worker` shows `(unhealthy)` but is still `Up` | Its event loop has stopped turning for more than two minutes. `docker restart bindery-worker`; in-flight claims are released on the way out or reclaimed by lease. See "When the worker goes unhealthy" |
| `bindery-worker` never leaves `(health: starting)` | It has not written its first heartbeat at all. The heartbeat needs no database and no network, so this means the process is not getting as far as starting its tasks — read `docker logs bindery-worker`, it will be an import or config failure |
| Any request redirects (302) to a Cloudflare login page | Somebody has re-created the Cloudflare Access application. It was removed deliberately (ADR-008) and it breaks scoped API tokens. Delete the application |
| API client gets `403 error code: 1010` | Cloudflare's Browser Integrity Check rejecting the default user agent. Send a real `User-Agent` header |
| Worker logs `relation "library" does not exist` at startup | It started before migrations were applied. It backs off and recovers on its own once the schema exists |
| Everything works, nothing classifies | Expected with no `ANTHROPIC_API_KEY`. The worker logs the deferral and retries |

## See also

- `D3 Cloud Vault/Bindery/ADR-006 — Cloudflare Tunnel and Access for Ingress.md`
- `docs/access-setup.md` — the invariants this deployment has to preserve
