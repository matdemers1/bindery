# Bindery

**Bindery finds the document you can't find.**

A self-hosted document archive that OCRs and indexes everything at the **page** level, decomposes 100-page bundled PDFs into their real constituent documents without ever modifying the original, and classifies them with Claude against a taxonomy it already knows.

> The problem: a 100-page military service bundle contains a DD-214 somewhere inside it, and no document manager on the market can tell you which page.

## Status

**Planned.** Full planning corpus lives in the Obsidian vault at `D3 Cloud Vault/Bindery/`. Nothing is implemented yet.

## Stack

| Layer | Choice |
|---|---|
| API | Python 3.13 · FastAPI · SQLAlchemy 2.0 · Alembic |
| Worker | Same image · OCRmyPDF 17 · Tesseract 5 · Ghostscript |
| Database | PostgreSQL 16 · `pg_trgm` · `pgvector` |
| Queue | Postgres `SELECT … FOR UPDATE SKIP LOCKED` — no Redis |
| Web | React 19 · Vite · Tailwind v4 (dark-first) |
| AI | Claude Opus 5 behind an `AIProvider` adapter · Batch API for backlog |
| Ingress | Cloudflare Tunnel + Access — **zero published host ports** |
| Host | ZimaOS · 16 GB RAM · AMD Ryzen · 16 TB |

## Quick start

```bash
cp .env.example .env      # fill in secrets
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml exec api alembic upgrade head
```

> Migrations are applied **explicitly**, never on container boot.

## Layout

```
api/      FastAPI application — REST, auth, search, blob serving
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
