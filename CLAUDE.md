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

```bash
docker compose -f infra/docker-compose.yml up -d          # start the stack
docker compose -f infra/docker-compose.yml exec api alembic upgrade head   # migrate (explicit)
docker compose -f infra/docker-compose.yml logs -f worker  # watch the pipeline
pytest                                                     # full suite
pytest tests/test_permissions.py                           # the leak suite — must always pass
pytest tests/test_ocr_accuracy.py                          # golden corpus scoring
```

## Conventions

- **Adapter pattern** for every external service (AI, notifications, future cloud replication)
- **Audit logging on all mutations** — ecosystem standard, and here it is also the trust surface
- **Prompt files are versioned** (`worker/ai/prompts/classify_v1.md`); the version is written to every classification row
- **AI calls are never live in the default test run** — tests use recorded responses
- Repository-layer permission scoping; no call site implements its own check

## ⚠️ Private repository

`tests/corpus/` holds real personal documents (DD-214, VA medical records, financial statements) as the golden corpus. **This repository must never be made public.**

## Planning Corpus

`D3 Cloud Vault/Bindery/` — Discovery Roadmap · Discovery & Requirements · Research Notes · Feature Ideas · Architecture · Data Model · Glossary · UX Flows & Screen Inventory · Risk Register · Test Strategy · Requirements Register (131 REQs) · Scope of Work (10 phases) · Phase Plans ×10 · ADR-001 … ADR-006

Start a coding session with `/start-development bindery`.
