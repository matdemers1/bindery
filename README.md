# Bindery

**Bindery finds the document you can't find.**

A self-hosted document archive that OCRs and indexes everything at the **page** level, decomposes 100-page bundled PDFs into their real constituent documents without ever modifying the original, and classifies them with Claude against a taxonomy it already knows.

> The problem: a 100-page military service bundle contains a DD-214 somewhere inside it, and no document manager on the market can tell you which page.

## Status

**Phase 3 — Classification & Review.** Drop a 100-page bundle into the watched folder. It is OCR'd, indexed page by page, cut into its real constituent documents, and catalogued. Search returns *the DD-214*, not the file containing it. Click any field and the why-panel quotes the sentence and page it came from, and says plainly what decided the filing.

Phases 1 and 2 involve **no AI at all**, which is the point: retrieval works on its own, and never depends on a third-party API being reachable.

> ⚠️ **Phase 3 was built with its own entry gate open.** The golden-corpus OCR accuracy figure has never been measured, so auto-file precision (REQ-058) is unscored and the gate's weights are reasoned rather than calibrated. No live Claude API call has been made either. Details in `D3 Cloud Vault/Bindery/Phase Plans/Phase 3 — Classification & Review.md`.

Also open: the Brother Scan-to-SMB spike (needs the scanner) and Phase 0's Cloudflare Tunnel setup (needs the dashboard).

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
| Ingress | Cloudflare Tunnel + Access — **zero published host ports** |
| Host | ZimaOS · 16 GB RAM · AMD Ryzen · 16 TB |

## Quick start

```bash
cp .env.example .env      # fill in secrets; set HOST_DATA_ROOT
make build
make up
make migrate
make seed-forms
make create-user email=you@example.com library=Household
```

Then `make tunnel` to bring up ingress, or reach the stack through Cloudflare at `$BINDERY_HOSTNAME`.

> Migrations are applied **explicitly**, never on container boot.

`make` exists only because the compose file lives in `infra/` and therefore needs
`--env-file` on every invocation. `make help`-free by design — read the Makefile,
it is twenty lines.

| Command | What it does |
|---|---|
| `make up` / `make down` | start / stop the stack (no tunnel) |
| `make tunnel` | start the stack including Cloudflare Tunnel ingress |
| `make ps` | the PORTS column must show no `host->container` mapping |
| `make migrate` | `alembic upgrade head`, explicitly |
| `make test` | default suite against a throwaway database |
| `make test-pipeline` | OCR and pipeline suites (needs the OCR toolchain) |
| `make ocr-report` | golden-corpus word accuracy — the R-01 gate |
| `make seed-forms` | load the known-form registry from `api/forms/seed/*.yaml` |
| `make enqueue-stage stage=segment` | re-run a pipeline stage over every file |
| `make reprocess prompt=v1` | re-classify documents left on an older prompt version |
| `make create-user email=… library=…` | there is no self-service registration |
| `make psql` / `make logs` / `make shell` | the usual |

## Layout

```
api/      FastAPI application — REST, auth, storage, search, blob serving
worker/   Pipeline consumer — ingest, OCR, page, segment, embed, classify, rules, file, mirror
web/      React 19 + Vite frontend
infra/    Docker Compose, Dockerfiles, Cloudflare Tunnel config
alembic/  Migrations
tests/    Unit, integration, permission (leak) suite, and the golden corpus
```

## ⚠️ This repository is private and must stay private

`tests/corpus/` contains **real personal documents** — a DD-214, VA medical records, and financial statements — used as the golden corpus for scoring OCR and classification quality. Never make this repository public.

## Documentation

All planning, architecture, and decisions live in the vault:
`D3 Cloud Vault/Bindery/` — Discovery, Research, Architecture, Data Model, Requirements Register (131 REQs), Scope of Work (10 phases), Risk Register, Test Strategy, ADR-001 … ADR-006, and ten Phase Plans.
