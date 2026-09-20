# Bindery

**Bindery finds the document you can't find.**

A self-hosted document archive that OCRs and indexes everything at the **page** level, decomposes 100-page bundled PDFs into their real constituent documents without ever modifying the original, and classifies them with Claude against a taxonomy it already knows.

> The problem: a 100-page military service bundle contains a DD-214 somewhere inside it, and no document manager on the market can tell you which page.

## Status

**Deployed and running**, on a ZimaOS host behind a Cloudflare Tunnel, holding a
real household corpus. Drop a 100-page bundle into the watched folder: it is
OCR'd, indexed page by page, cut into its real constituent documents, and
catalogued. Search returns *the DD-214*, not the file containing it. Click any
field and the why-panel quotes the sentence and page it came from, and says
plainly what decided the filing.

Retrieval involves **no AI at all**, which is the point: it works on its own, and
never depends on a third-party API being reachable. Classification is the only
thing that defers when there is no key.

**Which phases are built is recorded in one place —
`D3 Cloud Vault/Bindery/Scope of Work.md` — and deliberately not repeated here.**
A second copy is a copy that goes stale, and this section spent most of the
project's life proving it.

> ⚠️ **Phase 3 was built with its own entry gate open.** The golden-corpus OCR accuracy figure has never been measured, so auto-file precision (REQ-058) is unscored and the gate's weights are reasoned rather than calibrated. Details in `D3 Cloud Vault/Bindery/Phase Plans/Phase 3 — Classification & Review.md`.

Also open: the Brother Scan-to-SMB spike, which needs the scanner.

Full planning corpus lives in the Obsidian vault at `D3 Cloud Vault/Bindery/`.

## Stack

| Layer | Choice |
|---|---|
| API | Python 3.13 · FastAPI · SQLAlchemy 2.0 · Alembic |
| Worker | Same base image · OCRmyPDF 17 · Tesseract 5 · Ghostscript |
| Database | PostgreSQL 16 · `pg_trgm` · `pgvector` |
| Queue | Postgres `SELECT … FOR UPDATE SKIP LOCKED` — no Redis |
| Web | React 19 · Vite · Tailwind v4 (dark-first) |
| AI | Claude Opus 5 behind an `AIProvider` adapter · Batch API for backlog |
| Ingress | Cloudflare Tunnel — **zero published host ports** |
| Host | ZimaOS · 16 GB RAM · AMD Ryzen · 16 TB |

**Cloudflare Access was removed on 2026-08-30 (ADR-008).** The tunnel is now the
only thing in front of the application, so Bindery's own login page faces the
open internet and `api/auth/throttle.py` is load-bearing rather than defence in
depth. Nothing should describe this stack as having a second gate.

## Quick start

```bash
cp .env.example .env      # fill in secrets; set HOST_DATA_ROOT
make build
make up
make migrate
make seed-forms
docker compose --env-file .env -f infra/docker-compose.yml logs api | grep 'bindery setup'
```

Then open Bindery in a browser. A fresh install shows **Set up this archive**:
enter the setup code from that log, choose the owner's email and password,
enrol an authenticator, and save the recovery codes. That account is the
administrator. The code works once, expires in a day, and a new one is printed
with `docker compose … exec api python -m api.cli setup-code`.

The setup code is what stops whoever finds the URL first from owning the
archive: only someone who can read this host's container logs has it.

Then `make tunnel` to bring up ingress, and reach the stack through Cloudflare at
`$BINDERY_HOSTNAME`.

> Migrations are applied **explicitly**, never on container boot.

### Working on it locally, without a tunnel

There is no host port, by invariant (REQ-104), so `http://localhost:8080` reaches
nothing and never did. If you have no Cloudflare tunnel token — or simply want
the Vite dev server — this is the sanctioned way in: a socat sidecar on the
compose network, and the dev server proxying to it.

```bash
docker run -d --name bindery-apiproxy --network infra_default \
  -p 127.0.0.1:8000:8000 alpine/socat \
  tcp-listen:8000,fork,reuseaddr tcp-connect:api:8000
cd web && BINDERY_API=http://127.0.0.1:8000 npm run dev
```

`npm run dev` has to run on the host rather than in a container, because
`node_modules` is built for the host's platform — which is the whole reason the
proxy target is overridable. See `web/vite.config.ts`.

## Commands

`make` exists only because the compose file lives in `infra/` and therefore needs
`--env-file` on every invocation. **`make help` lists every target**; the table
below is the short version.

| Command | What it does |
|---|---|
| `make up` / `make down` | start / stop the stack (no tunnel) |
| `make tunnel` | start the stack including Cloudflare Tunnel ingress |
| `make ps` | the PORTS column must show no `host->container` mapping |
| `make migrate` | `alembic upgrade head`, explicitly |
| `make test` | default suite against a throwaway database |
| `make test-pipeline` | OCR and pipeline suites (needs the OCR toolchain) |
| `make typecheck` | mypy over `api/` and `worker/`, against a baseline that only shrinks |
| `make contract` | `web/src/api.ts` against the server's response models |
| `make lint-web` | eslint and `tsc --noEmit` for the web app and the e2e specs |
| `make screenshots` | recapture the documentation screenshots `tests/test_docs.py` gates |
| `make ocr-report` | golden-corpus word accuracy — the R-01 gate |
| `make seed-forms` | load the known-form registry from `api/forms/seed/*.yaml` |
| `make backup` / `make drill` | take a backup; then restore it and find the DD-214 |
| `make drill-offsite` | the same from S3 alone — no local backup, no live stack |
| `make lifecycle-check` | audit the offsite bucket's expiry rules by hand (the worker also does it hourly) |
| `make enqueue-stage stage=segment` | re-run a pipeline stage over every file |
| `make reprocess prompt=v1` | re-classify documents left on an older prompt version |
| `make create-user email=… library=… [owner=1]` | scripted accounts (CI, tests); a fresh install is claimed in the browser instead, and `owner=1` makes a scripted first account finish setup the same way |
| `make psql` / `make logs` / `make shell` | the usual |

## Deploying to the ZimaOS host

`docs/zimaos-deploy.md` is the runbook. In short: CI publishes images to GHCR on
push to `main`, the Zima pulls them, and `infra/zimaos/bindery.zimaos.yaml` is
pasted into ZimaOS → Apps → Custom Install.

Deploy stays a deliberate manual pull-and-restart — nothing auto-updates the
thing holding your passport.

CI (`.github/workflows/build.yml`) is five gates in series — **lint → unit →
integration → e2e → images** — cheapest first, and nothing is published until
every one of them passes.

## Layout

```
api/        FastAPI application — REST, auth, storage, search, blob serving
worker/     Pipeline consumer — ingest, OCR, page, segment, embed, classify, rules, file, mirror
web/        React 19 + Vite frontend
infra/      Docker Compose, Dockerfiles, Cloudflare Tunnel config, AWS lifecycle rules
alembic/    Migrations
tests/      Unit, integration, permission (leak) suite, and the golden corpus
scripts/    Restore drill, screenshot capture, the typecheck and contract gates
docs/       Operator runbooks: deploy, backup and restore, offsite replication
```

## ⚠️ This repository is private and must stay private

`tests/corpus/` is the golden corpus used to score OCR and classification quality. It holds **real personal documents** — a DD-214, VA medical records, financial statements — and they are **not in this repository**: everything under that path is gitignored except the harness. The fixtures stay on the maintainer's machine, so `make ocr-report` is reproducible only against a corpus you supply yourself.

## Documentation

All planning, architecture, and decisions live in the vault:
`D3 Cloud Vault/Bindery/` — Discovery, Research, Architecture, Data Model,
Requirements Register, Scope of Work, Risk Register, Test Strategy, the ADRs and
the Phase Plans. Counts are deliberately not repeated here: the vault is not part
of this repository, so nothing in CI can check them, and every count this file
used to carry was wrong.

`CLAUDE.md` is the working conventions and the hard-won gotchas.
`web/public/help/guides.json` is the in-app documentation, and
`tests/test_docs.py` fails the build when it drifts from the screens it describes.

## License

Apache-2.0 — see [LICENSE](LICENSE). The code is open; the archive it serves is not.
