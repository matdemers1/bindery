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

## 1. A GitHub token the Zima can pull with

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

This writes `~/.docker/config.json` on the host and persists across reboots.

---

## 2. Storage on the 16 TB pool

```bash
mkdir -p /DATA/AppData/bindery/data/inbox/household
mkdir -p /DATA/AppData/bindery/pgdata
```

`data/` holds blobs, derived artifacts, the watched-folder inbox, and exports.
`pgdata/` holds the database. Both are on the pool, not the boot device.

> If your pool is mounted somewhere other than `/DATA`, change it here **and** in
> the two volume paths in the compose file.

The inbox subdirectory name must match a library name — `household` matches a
library called "Household". That is how a scanner picks a destination.

---

## 3. Cloudflare Tunnel

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

## 4. Cloudflare Access

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

## 5. Install the app on ZimaOS

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

## 6. First run

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

## 7. Verify

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

## 8. A backup, before you trust it with anything

Not the Phase 6 answer. Enough that a disk failure this month is survivable.

```bash
mkdir -p /DATA/Backups/bindery
cat > /usr/local/bin/bindery-backup.sh <<'SH'
#!/bin/sh
set -eu
DEST=/DATA/Backups/bindery
STAMP=$(date +%F)
docker exec bindery-postgres pg_dump -U bindery -Fc bindery > "$DEST/bindery-$STAMP.dump"
# Blobs are content-addressed and write-once, so this only ever adds.
rsync -a --ignore-existing /DATA/AppData/bindery/data/blobs/ "$DEST/blobs/"
# Keep a month of database dumps. Blobs are never pruned.
find "$DEST" -name 'bindery-*.dump' -mtime +30 -delete
SH
chmod +x /usr/local/bin/bindery-backup.sh
(crontab -l 2>/dev/null; echo "30 3 * * * /usr/local/bin/bindery-backup.sh") | crontab -
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
| Everything works, nothing classifies | Expected with no `ANTHROPIC_API_KEY`. The worker logs the deferral and retries |

## See also

- `D3 Cloud Vault/Bindery/ADR-006 — Cloudflare Tunnel and Access for Ingress.md`
- `docs/access-setup.md` — the invariants this deployment has to preserve
