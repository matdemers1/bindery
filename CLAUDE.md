# CLAUDE.md — Bindery

Self-hosted document archive. **Read `D3 Cloud Vault/Bindery/` before doing any work** — the planning corpus there is the source of truth for every decision below.

## What Bindery Is

Bindery finds the document you can't find. It OCRs and indexes at the **page** level, decomposes bundled PDFs into their real constituent documents without modifying originals, and classifies with Claude against a taxonomy it already knows.

**Success criterion:** find the DD-214 in under 10 seconds.
**Kill criterion:** loss of trust in the automated filing.

## The Governing Principle — Auditable Automation

> Automate by default; make every automated decision cheap to inspect and one click to reverse.

Trust is earned after the fact, not by asking permission first. This is a **constraint on every feature that touches AI output**:

- Provenance on every AI-written field — which prompt version, which model, what confidence, which source snippet and page
- Append-only audit trail on all mutations, human and machine
- Undo for every automated action, including un-filing and un-segmenting
- Confidence surfaced in the UI, not only used at the gate

## Non-Negotiable Invariants

Breaking any of these is a bug, not a tradeoff:

1. **Originals are never modified.** Content-addressed, immutable, write-once. Exports are derivative artifacts.
2. **A document is a page range over a source file** — `(source_file_id, page_start, page_end)`. Bundles are sliced virtually. There is no separate segment table.
3. **Nothing is ever automatically deleted.** No unattended destructive code path may exist.
4. **Library is the access boundary.** Every document belongs to exactly one. Filtering happens at the repository layer, never at individual call sites.
5. **Auto-file gating reads structural signals, not the model's self-reported confidence.** LLM confidence is poorly calibrated; gate on known-form matches, `existing_ids`-only tags, pre-existing correspondents, labeled dates, and rule hits.
6. **Reused taxonomy resolves by ID and never passes through normalization or translation.** `existing_ids` and `new_names` are separate response fields.
7. **Retrieval never depends on the Claude API.** A document ingested during an outage is OCR'd, paged, and fully searchable — only classification defers.
8. **Nothing fails silently.** Every failed document surfaces somewhere a human will see it.
9. **No published host ports.** Ingress is Cloudflare Tunnel only.
10. **Migrations are applied explicitly**, never on container boot.

## Stack

Python 3.13 · FastAPI · SQLAlchemy 2.0 (async) · Alembic · PostgreSQL 16 (`pg_trgm`, `pgvector`) · OCRmyPDF 17 + Tesseract 5 · React 19 + Vite + Tailwind v4 (dark-first) · Docker Compose · Cloudflare Tunnel + Access · Claude Opus 5 behind an `AIProvider` adapter.

**No Redis.** The job queue is Postgres `SELECT … FOR UPDATE SKIP LOCKED`, and the `job` table is the observability surface.

## Pipeline

`ingest → normalize (OCR) → page → segment → embed → classify → rules → file → mirror`

**Every stage persists its output artifact and is independently replayable**, keyed on `(source_file_id, stage, prompt_version)`. Re-classify without re-OCR. Re-segment without re-ingest. This is what makes prompt and OCR iteration affordable — do not break it.

## Dev Commands

The compose file lives in `infra/`, so every raw invocation needs
`--env-file .env` to pick up the repo-root env. The Makefile exists only to carry
that flag — use it.

```bash
make up            # start the stack (no tunnel); `make tunnel` adds ingress
make migrate       # alembic upgrade head — explicit, never on boot
make logs          # watch the pipeline
make test          # full suite, against a throwaway bindery_test database
make ps            # no row may show a host->container port mapping (REQ-104)
make create-user email=you@example.com library=Household
```

Tests run **inside the stack**, not on the host: there are no published ports, so
the database is unreachable from outside. `make test` uses the `dev` target of
`Dockerfile.api` and a separate `bindery_test` database.

```bash
make test                                    # everything
docker compose --env-file .env -f infra/docker-compose.yml --profile test \
  run --rm test python -m pytest tests/test_permissions.py   # the leak suite (Phase 7)
```

## Current State — Phase 1 built except the two hardware-bound tasks

**Phase 0** is complete except **T-0.5** (Cloudflare Tunnel + Access service
token), which needs the dashboard.

**Phase 1** — retrieval, no AI — is built and verified end to end: a 100-page
bundle dropped into the watched folder OCRs, pages, and is searchable; ⌘K →
`dd214` → Enter opens the viewer on page 47 with the term highlighted.

Open in Phase 1:
- **T-1.12** — the golden-corpus OCR accuracy figure. The scorer and report are
  built; the R-01 gate needs *real* documents in `tests/corpus/`. The test skips
  loudly rather than passing on synthetic pages.
- **T-1.13** — the Brother Scan-to-SMB spike. Needs the physical scanner.

Not started: Phase 2 (documents as page ranges, known forms).

## Layering rule

`worker/` may import `api/`; **`api/` must never import `worker/`.** Anything
both processes need — config, models, session, queue, blob store, ingest —
lives in `api/`, because that is the only package both images carry. `worker/`
holds the pipeline entrypoint, its stages, and the ingest adapters.

## Pipeline shape today

`watched folder | upload → normalize → page` — segment, embed, classify, rules,
file and mirror are later phases and are deliberately absent from `STAGES`, so a
job naming one dead-letters rather than silently succeeding.

## Conventions

- **Adapter pattern** for every external service (AI, notifications, future cloud replication)
- **Audit logging on all mutations** — ecosystem standard, and here it is also the trust surface
- **Prompt files are versioned** (`worker/ai/prompts/classify_v1.md`); the version is written to every classification row
- **AI calls are never live in the default test run** — tests use recorded responses
- Repository-layer permission scoping in `api/db/repository.py`; no call site implements its own check
- **Enum columns use `pg_enum()` from `api/db/base.py`**, which persists member
  *values*. Plain `sa.Enum(SomeEnum)` writes member *names* and will not match
  the lowercase types the migrations create.
- `tests/test_no_destructive_paths.py` enforces REQ-090 by scanning `api/` and
  `worker/`. If it fails, revoke or tombstone — do not loosen the pattern list.

## ⚠️ Private repository

`tests/corpus/` holds real personal documents (DD-214, VA medical records, financial statements) as the golden corpus. **This repository must never be made public.**

## Planning Corpus

`D3 Cloud Vault/Bindery/` — Discovery Roadmap · Discovery & Requirements · Research Notes · Feature Ideas · Architecture · Data Model · Glossary · UX Flows & Screen Inventory · Risk Register · Test Strategy · Requirements Register (131 REQs) · Scope of Work (10 phases) · Phase Plans ×10 · ADR-001 … ADR-006

Start a coding session with `/start-development bindery`.
