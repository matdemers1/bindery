# Deploying Bindery to the ZimaOS host

Everything here is done once. After that a deploy is `docker compose pull &&
docker compose up -d`, and you choose when it happens — nothing auto-updates the
thing holding your passport.

**Host:** ZimaOS at `192.168.1.231`
**Hostname:** `bindery.d3cloud.io` (Cloudflare Tunnel — no ports opened on the Zima)

---

## 0. Before you start — read this part

Two things are true of this deployment on day one, and both are deliberate:

- **There is no verified backup yet.** 3-2-1 backup and the restore drill are
  Phase 6 (T-6.6, T-6.7). A single host with a single disk pool losing the
  archive is risk **R-09**. Step 8 below sets up a nightly dump as an interim
  measure — it is *not* the drill, because a backup nobody has restored from is
  a hypothesis.
- **OCR accuracy on your real documents has never been measured** (T-1.12, the
  R-01 gate). Until it is, treat classification output as unproven.

Until the restore drill passes, **feed it copies, not originals you would miss.**
The originals stay byte-identical on disk either way — that is invariant 1 — but
"the disk" is currently one disk.

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

Then, on the Zima — over SSH (`ssh root@192.168.1.231`) or the terminal in the
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
```

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

## 5. Cloudflare Access

The tunnel makes it reachable; Access decides who reaches it. Bindery's own JWT
login exists regardless — Access is defence in depth, never the only gate
(ADR-006).

**Zero Trust → Access → Applications → Add an application → Self-hosted:**

- Application domain: `bindery.d3cloud.io`
- Policy: *Allow*, rule `Emails` → `matthew@demers.dev`

### The service-token path — do not skip this

Access expects a browser identity flow, which breaks scoped API tokens and any
future native client. This is risk **R-15**, and it is certain to bite rather
than merely possible.

1. **Access → Service Auth → Create Service Token**, name it `bindery-api`. Copy
   the client id and secret — the secret is shown once.
2. Add a **second policy** to the application:
   - Action: *Service Auth*
   - Rule: `Service Token` → `bindery-api`
   - **Precedence above the Allow policy**
3. Verify it works without a browser:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "CF-Access-Client-Id: <id>" \
  -H "CF-Access-Client-Secret: <secret>" \
  https://bindery.d3cloud.io/api/health
```

`200` means the token path works. A `302` to a login page means the policy is
below the Allow policy, or scoped to the wrong path.

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

# There is no self-service registration.
docker exec -it bindery-api python -m api.cli create-user \
  --email matthew@demers.dev --library "Household"
```

---

## 8. Verify

```bash
# REQ-104: no service may publish a host port. No row may contain "->".
docker ps --filter name=bindery --format '{{.Names}}\t{{.Ports}}'

# All five up, four reporting healthy.
docker ps --filter name=bindery --format '{{.Names}}\t{{.Status}}'

# The pipeline is watching.
docker logs bindery-worker --tail 20   # expect "watching /data/inbox"
```

Then, from a browser: <https://bindery.d3cloud.io> should challenge you with
Access, then show Bindery's own login.

**The real test** — drop a PDF into `/DATA/AppData/bindery/data/inbox/household/`
and watch it move:

```bash
docker logs -f bindery-worker
```

It should ingest (after the stability check), normalize, page, and segment. Then
press ⌘K in the browser and search for a word you know is inside it.

---

## 9. A backup, before you trust it with anything

Not the Phase 6 answer. Enough that a disk failure this month is survivable.

Installed at `/DATA/AppData/bindery/backup.sh`, running nightly at 03:30. It
dumps Postgres and rsyncs blobs to `/media/Main-Storage/Backups/bindery/` — the
RAID array, a different device from the NVMe the database sits on.

Verify a dump is genuinely restorable rather than merely non-empty:

```bash
DUMP=$(ls -t /media/Main-Storage/Backups/bindery/*.dump | head -1)
docker exec -i bindery-postgres pg_restore -l < "$DUMP" | grep -c "TABLE DATA"
```

`derived/` is deliberately not backed up — every byte of it is reproducible from
a blob by re-running the pipeline. Losing it costs CPU, not data.

> This is still one machine. Getting a copy **off** the Zima is T-6.6, and until
> a restore has actually been performed (T-6.7) none of this is proven.

---

## Deploying a change afterwards

Push to `main`, wait for the build, then on the Zima:

```bash
docker compose -f /var/lib/casaos/apps/bindery/docker-compose.yml pull
docker compose -f /var/lib/casaos/apps/bindery/docker-compose.yml up -d
docker exec bindery-api alembic upgrade head   # only if the release adds one
```

To roll back, change the `:main` tags to `:sha-<commit>` and `up -d` again.

> **Back up before any migration.** R-10 is a bad migration corrupting the
> archive, and the mitigation is a dump taken immediately beforehand plus a
> rehearsal on the staging stack (`make` targets in the repo).

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `manifest unknown` / `not found` on pull | Step 1 not done, or the token lacks `read:packages` |
| Browser reaches Access but then a 502 | `web` is unhealthy, or the tunnel points somewhere other than `web:80` |
| Files sit in the inbox untouched | The subdirectory name does not match a library name; check `docker logs bindery-worker` |
| Jobs queued but never claimed | Worker cannot reach the database — compare `DATABASE_URL` between the api and worker blocks |
| `curl` with a service token returns 302 | The Service Auth policy is below the Allow policy in precedence |
| API client gets `403 error code: 1010` | Cloudflare's Browser Integrity Check rejecting the default user agent. Send a real `User-Agent` header |
| Worker logs `relation "library" does not exist` at startup | It started before migrations were applied. It backs off and recovers on its own once the schema exists |
| Everything works, nothing classifies | Expected with no `ANTHROPIC_API_KEY`. The worker logs the deferral and retries |

## See also

- `D3 Cloud Vault/Bindery/ADR-006 — Cloudflare Tunnel and Access for Ingress.md`
- `docs/access-setup.md` — the invariants this deployment has to preserve
